"""
composer.py — All LLM logic: compose(), compose_reply(), call_llm(), parse_llm_response().
LLM stack: Gemini Flash Latest (Primary) -> Groq llama-3.3-70b (Fallback) -> Template Fallback.
Uses official google-generativeai SDK.
"""
import os
import re
import json
import logging
import asyncio
import httpx
import google.generativeai as genai
from typing import Optional

from store import get, get_turns, is_suppressed
from prompts import (
    TRIGGER_KIND_MAP,
    build_system_prompt,
    build_user_prompt,
    build_reply_prompt,
)

logger = logging.getLogger(__name__)

# ── LLM Configuration ─────────────────────────────────────────────────────────

GEMINI_MODEL_NAME = "gemini-2.0-flash"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_TIMEOUT = 8.0
GROQ_TIMEOUT = 6.0

# ── Phrase lists (from spec §6.5) ─────────────────────────────────────────────

_AUTO_REPLY_PHRASES = [
    "thank you for contacting", "our team will respond",
    "automated assistant", "ek automated", "main ek automated",
    "yeh ek automated", "aapki madad ke liye shukriya, lekin main",
]

_OPT_OUT_PHRASES = [
    "stop messaging", "not interested", "band karo", "mat bhejo",
    "unsubscribe", "don't message", "leave me alone", "go away",
]

_ACTION_PHRASES = [
    "let's do it", "lets do it", "go ahead", "haan karo", "kar do",
    "ok do it", "yes please", "yes sure", "bilkul", "definitely",
    "confirm", "send it", "yes", "go",
]


# ── Core compose function (for /v1/tick) ─────────────────────────────────────

async def compose(trigger_payload: dict, now: str) -> Optional[dict]:
    """
    Given a trigger payload, build a full dynamic prompt and call LLM.
    Returns an action dict ready for tick response, or None to skip.
    """
    # 1. Resolve IDs from payload (Spec can be inconsistent on nesting)
    trigger_kind = trigger_payload.get("kind", "generic")
    trigger_id = trigger_payload.get("id") or trigger_payload.get("payload", {}).get("id", "unknown")
    merchant_id = trigger_payload.get("merchant_id") or trigger_payload.get("payload", {}).get("merchant_id", "")
    customer_id = trigger_payload.get("customer_id") or trigger_payload.get("payload", {}).get("customer_id")
    suppression_key = trigger_payload.get("suppression_key", "")

    if not merchant_id:
        logger.warning("compose: missing merchant_id in trigger %s", trigger_id)
        return None

    # 2. Load context data directly from store.get()
    merchant_data = get("merchant", merchant_id)
    if not merchant_data:
        logger.warning("compose: merchant %s not found in store", merchant_id)
        return None
    
    category_slug = merchant_data.get("category_slug", "")
    category_data = get("category", category_slug) or {}

    customer_data = None
    if customer_id:
        customer_data = get("customer", customer_id)

    # 2. TOP-10: Category-aware restraint logic
    if trigger_kind == "ipl_match_today":
        if category_slug not in ["restaurants"]:
            logger.info("compose: restraint - IPL irrelevant for %s", category_slug)
            return None

    # 3. Resolve research digest item
    digest_item = None
    top_item_id = trigger_payload.get("payload", {}).get("top_item_id")
    if top_item_id and category_data.get("digest"):
        for item in category_data["digest"]:
            if item.get("id") == top_item_id:
                digest_item = item
                break

    # 4. Build Prompts
    system_prompt = build_system_prompt(category_slug, category_data)
    user_prompt = build_user_prompt(
        kind=trigger_kind,
        merchant=merchant_data,
        trigger=trigger_payload,
        customer=customer_data,
        digest_item=digest_item,
        merchant_id=merchant_id,
        category=category_data,
    )

    # 5. Call LLM
    response_text = await call_llm(system_prompt, user_prompt)
    if not response_text:
        return build_fallback_action(merchant_data, trigger_payload, merchant_id)

    action_data = parse_llm_response(response_text)
    if not action_data or not action_data.get("body"):
        return build_fallback_action(merchant_data, trigger_payload, merchant_id)

    # 6. Enforce hard constraints
    body_text = enforce_body_constraints(action_data["body"])

    return {
        "conversation_id": f"conv_{merchant_id}_{trigger_id}",
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": "merchant_on_behalf" if customer_id else "vera",
        "trigger_id": trigger_id,
        "template_name": f"vera_{trigger_kind}_v1",
        "template_params": extract_template_params(action_data, merchant_data),
        "body": body_text,
        "cta": action_data.get("cta", "open_ended"),
        "suppression_key": suppression_key,
        "rationale": action_data.get("rationale", f"{trigger_kind} trigger processed"),
    }


# ── Reply composer (for /v1/reply) ────────────────────────────────────────────

async def compose_reply(
    conversation_id: str,
    merchant_id: str,
    customer_id: Optional[str],
    message: str,
    turn_number: int,
) -> dict:
    """Handles all /v1/reply scenarios with full spec compliance."""
    history = get_turns(conversation_id)
    msg_lower = message.lower().strip()

    # Load merchant data
    merchant_data = get("merchant", merchant_id) or {}
    category_slug = merchant_data.get("category_slug", "")
    category_data = get("category", category_slug) or {}

    # 1. Auto-reply logic
    is_auto = any(phrase in msg_lower for phrase in _AUTO_REPLY_PHRASES)
    if is_auto:
        # Turn 1 should ALWAYS be Tier 1, regardless of history (safety reset)
        if turn_number <= 1:
             return {"action": "send", "body": "Looks like an auto-reply 😊 Reply 'Yes' to continue.", "cta": "binary_yes_no", "rationale": "Auto-reply Tier 1 (Turn 1 Reset)"}

        # Count consecutive auto-replies in current history
        consecutive_autos = 0
        for t in reversed(history):
            if t.get("role") == "merchant" and any(p in t.get("text", "").lower() for p in _AUTO_REPLY_PHRASES):
                consecutive_autos += 1
            else:
                break
        
        # Current message is an auto-reply, so add it
        auto_tier = consecutive_autos + 1
        
        if auto_tier == 1:
            return {"action": "send", "body": "Looks like an auto-reply 😊 Reply 'Yes' to continue.", "cta": "binary_yes_no", "rationale": "Auto-reply Tier 1"}
        elif auto_tier == 2:
            return {"action": "wait", "wait_seconds": 86400, "rationale": "Auto-reply Tier 2"}
        else:
            return {"action": "end", "rationale": "Auto-reply Tier 3"}

    # 2. Opt-out logic
    if any(p in msg_lower for p in _OPT_OUT_PHRASES):
        return {"action": "end", "rationale": "Merchant opted out."}

    # 3. Intent transition
    is_action = any(p in msg_lower for p in _ACTION_PHRASES)

    # 4. LLM Generation
    system_prompt = build_system_prompt(category_slug, category_data)
    user_prompt = build_reply_prompt(
        message=message,
        history=history,
        merchant=merchant_data,
        category_slug=category_slug,
        category=category_data,
        is_action=is_action,
    )

    response_text = await call_llm(system_prompt, user_prompt)
    if not response_text:
        return {"action": "send", "body": "Got it, I'll follow up shortly.", "cta": "none", "rationale": "LLM fallback"}

    action_data = parse_llm_response(response_text)
    if action_data and action_data.get("action") == "send" and action_data.get("body"):
        action_data["body"] = enforce_body_constraints(action_data["body"])
    
    return action_data or {"action": "end", "rationale": "Parse failure"}


# ── LLM callers ───────────────────────────────────────────────────────────────

async def call_llm(system: str, user: str) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key: 
        logger.warning("call_llm: No GEMINI_API_KEY found in environment")
        return None
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name=GEMINI_MODEL_NAME, system_instruction=system)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(
                user,
                generation_config=genai.types.GenerationConfig(temperature=0.1, max_output_tokens=1024, response_mime_type="application/json"),
                safety_settings={"HARM_CATEGORY_HARASSMENT": "BLOCK_NONE", "HARM_CATEGORY_HATE_SPEECH": "BLOCK_NONE", "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE", "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_NONE"}
            )
        )
        if response and response.text: return response.text
        return await _call_groq(system, user)
    except Exception as e:
        logger.warning("Gemini call failed: %s", e)
        return await _call_groq(system, user)

async def _call_groq(system: str, user: str) -> Optional[str]:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key: return None
    try:
        payload = {"model": GROQ_MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.0, "response_format": {"type": "json_object"}}
        async with httpx.AsyncClient(timeout=GROQ_TIMEOUT) as client:
            resp = await client.post(GROQ_ENDPOINT, json=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            if resp.status_code == 200: return resp.json()["choices"][0]["message"]["content"]
        return None
    except Exception: return None


# ── Utilities ─────────────────────────────────────────────────────────────────

def parse_llm_response(text: str) -> Optional[dict]:
    if not text: return None
    text = text.strip()
    try: return json.loads(text)
    except: pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try: return json.loads(match.group())
        except: pass
    return None

def enforce_body_constraints(body: str) -> str:
    body = re.sub(r"https?://\S+|www\.\S+", "", body)
    return re.sub(r" {2,}", " ", body).strip()[:320]

def build_fallback_action(merchant_data: dict, trigger: dict, merchant_id: str) -> dict:
    identity = merchant_data.get("identity", {})
    owner = identity.get("owner_first_name", "") or identity.get("name", "Merchant")
    offers = [o["title"] for o in merchant_data.get("offers", []) if o.get("status") == "active"]
    offer_str = offers[0] if offers else "your best offer"
    return {
        "conversation_id": f"conv_{merchant_id}_{trigger.get('id')}",
        "merchant_id": merchant_id,
        "customer_id": trigger.get("customer_id"),
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_fallback_v1",
        "template_params": [owner, offer_str],
        "body": f"{owner}, your {offer_str} is live — want to push it?",
        "cta": "binary_yes_no",
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": "LLM fallback",
    }

def extract_template_params(action: dict, merchant_data: dict) -> list[str]:
    identity = merchant_data.get("identity", {})
    owner = identity.get("owner_first_name", "") or identity.get("name", "")
    return [owner, action.get("body", "")[:100], action.get("cta", "")]
