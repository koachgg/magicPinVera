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

GEMINI_MODEL_NAME = "gemini-flash-latest"
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
    Given a trigger payload (from store), build a full dynamic prompt and call LLM.
    Returns an action dict ready for tick response, or None to skip.
    """
    trigger = trigger_payload
    merchant_id: str = trigger.get("merchant_id", "")
    customer_id: Optional[str] = trigger.get("customer_id")
    kind: str = trigger.get("kind", "generic")
    trigger_id: str = trigger.get("id", "")

    # 1. Load merchant context
    merchant = get("merchant", merchant_id)
    if not merchant:
        logger.warning("compose: no merchant context for %s", merchant_id)
        return None

    # 2. Load category context
    category_slug: str = merchant.get("category_slug", "")
    category: dict = get("category", category_slug) or {}

    # 3. Load customer context
    customer: Optional[dict] = None
    if customer_id:
        customer = get("customer", customer_id)

    # 4. Check suppression
    suppression_key: str = trigger.get("suppression_key", "")
    if suppression_key and is_suppressed(suppression_key):
        logger.info("compose: suppressed key=%s", suppression_key)
        return None

    # 5. TOP-10: Restraint logic — skip weak signals
    # Example: IPL trigger when merchant data is trending negative
    if kind == "ipl_match_today":
        delta_7d = merchant.get("performance", {}).get("delta_7d", {})
        if delta_7d.get("views_pct", 0) < -0.05:
            logger.info("compose: restraint applied for %s (negative trend on event trigger)", trigger_id)
            return None  # Restraint rewarded by spec

    # 6. Resolve digest item
    digest_item = None
    top_item_id = trigger.get("payload", {}).get("top_item_id")
    if top_item_id and category.get("digest"):
        for item in category["digest"]:
            if item.get("id") == top_item_id:
                digest_item = item
                break

    # 7. Build dynamic prompts (NO HARDCODING)
    system_prompt = build_system_prompt(category_slug, category)
    user_prompt = build_user_prompt(
        kind=kind,
        merchant=merchant,
        trigger=trigger,
        customer=customer,
        digest_item=digest_item,
        merchant_id=merchant_id,
        category=category,
    )

    # 8. Call LLM
    response_text = await call_llm(system_prompt, user_prompt)
    if not response_text:
        return build_fallback_action(merchant, trigger, merchant_id)

    # 9. Parse LLM output
    action = parse_llm_response(response_text)
    if not action or not action.get("body"):
        return build_fallback_action(merchant, trigger, merchant_id)

    # 10. Enforce hard constraints
    body = enforce_body_constraints(action["body"])

    return {
        "conversation_id": f"conv_{merchant_id}_{trigger_id}",
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": "merchant_on_behalf" if customer_id else "vera",
        "trigger_id": trigger_id,
        "template_name": f"vera_{kind}_v1",
        "template_params": extract_template_params(action, merchant),
        "body": body,
        "cta": action.get("cta", "open_ended"),
        "suppression_key": suppression_key,
        "rationale": action.get("rationale", f"{kind} trigger processed"),
    }


# ── Reply composer (for /v1/reply) ────────────────────────────────────────────

async def compose_reply(
    conversation_id: str,
    merchant_id: str,
    customer_id: Optional[str],
    message: str,
    turn_number: int,
) -> dict:
    """
    Handles all /v1/reply scenarios with full spec compliance:
    - Auto-reply: 3-tier escalation (send → wait 24h → end)
    - Opt-out: immediate end
    - Intent transition: action mode (no more qualifying questions)
    - Default: continue conversation naturally via LLM
    """
    history = get_turns(conversation_id)
    merchant = get("merchant", merchant_id) or {}
    category_slug = merchant.get("category_slug", "")
    category = get("category", category_slug) or {}

    msg_lower = message.lower().strip()

    # ── GAP 1 & 2: 3-tier auto-reply escalation ──────────────────────────────
    is_auto = any(phrase in msg_lower for phrase in _AUTO_REPLY_PHRASES)
    if is_auto:
        # Count consecutive auto-replies in history (including current one)
        auto_count = sum(
            1 for t in reversed(history[-5:])
            if t.get("role") == "merchant" and
            any(p in t.get("text", "").lower() for p in _AUTO_REPLY_PHRASES)
        )
        if auto_count <= 1:
            # First auto-reply: Flag it to owner
            return {
                "action": "send",
                "body": "Looks like an auto-reply 😊 When the owner sees this, just reply 'Yes' to continue.",
                "cta": "binary_yes_no",
                "rationale": "First auto-reply detected; prompting owner"
            }
        elif auto_count == 2:
            # Tier 2: Wait 24h
            return {
                "action": "wait",
                "wait_seconds": 86400,
                "rationale": "Second consecutive auto-reply; owner not at phone. Waiting 24h."
            }
        else:
            # Tier 3: End conversation
            return {
                "action": "end",
                "rationale": "Three or more consecutive auto-replies. Zero engagement. Closing."
            }

    # ── GAP 4: Expanded opt-out detection ─────────────────────────────────────
    if any(p in msg_lower for p in _OPT_OUT_PHRASES):
        return {
            "action": "end",
            "rationale": "Merchant explicitly opted out. Suppressing all future messages."
        }

    # ── GAP 3: Intent transition detection ────────────────────────────────────
    is_action = any(p in msg_lower for p in _ACTION_PHRASES)

    # Build reply prompt (all in prompts.py — no hardcoding)
    user_prompt = build_reply_prompt(
        message=message,
        history=history,
        merchant=merchant,
        category_slug=category_slug,
        category=category,
        is_action=is_action,
    )
    system_prompt = build_system_prompt(category_slug, category)

    response_text = await call_llm(system_prompt, user_prompt)
    if not response_text:
        return {
            "action": "send",
            "body": "Got it — I'll follow up shortly.",
            "cta": "none",
            "rationale": "LLM fallback"
        }

    parsed = parse_llm_response(response_text)
    if parsed and parsed.get("action") == "send" and parsed.get("body"):
        parsed["body"] = enforce_body_constraints(parsed["body"])
    return parsed or {"action": "end", "rationale": "Parse failure — closing safely"}


# ── LLM callers ───────────────────────────────────────────────────────────────

async def call_llm(system: str, user: str) -> Optional[str]:
    # Attempt 1: Gemini
    result = await _call_gemini(system, user)
    if result: return result

    # Attempt 2: Gemini retry after brief wait
    await asyncio.sleep(2.0)
    result = await _call_gemini(system, user)
    if result: return result

    # Attempt 3: Groq fallback
    return await _call_groq(system, user)


async def _call_gemini(system: str, user: str) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set")
        return None
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name=GEMINI_MODEL_NAME, system_instruction=system)

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(
                user,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.1,
                    max_output_tokens=1024,
                    response_mime_type="application/json",
                ),
                safety_settings={
                    "HARM_CATEGORY_HARASSMENT": "BLOCK_NONE",
                    "HARM_CATEGORY_HATE_SPEECH": "BLOCK_NONE",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE",
                    "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_NONE",
                }
            )
        )

        # OBSERVABILITY: log finish reason
        if response.candidates:
            finish_reason = response.candidates[0].finish_reason.name
            if finish_reason != "STOP":
                logger.warning("Gemini finish_reason=%s (possible safety block)", finish_reason)

        if response and response.text:
            return response.text
        return None
    except Exception as exc:
        logger.warning("Gemini SDK Error: %s", exc)
        return None


async def _call_groq(system: str, user: str) -> Optional[str]:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key: return None
    try:
        payload = {
            "model": GROQ_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.0,
            "response_format": {"type": "json_object"}
        }
        async with httpx.AsyncClient(timeout=GROQ_TIMEOUT) as client:
            resp = await client.post(
                GROQ_ENDPOINT, json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
        return None
    except Exception:
        return None


# ── Utilities (Parsing & Constraints) ─────────────────────────────────────────

def parse_llm_response(text: str) -> Optional[dict]:
    if not text: return None
    text = text.strip()

    try: return json.loads(text)
    except: pass

    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try: return json.loads(cleaned)
    except: pass

    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try: return json.loads(match.group())
        except: pass

    logger.error("parse_llm_response: FAILED. Length=%d. Raw: %s", len(text), text[:1000])
    return None


def enforce_body_constraints(body: str) -> str:
    body = re.sub(r"https?://\S+|www\.\S+", "", body)
    body = re.sub(r" {2,}", " ", body).strip()
    return body[:320]


# ── TOP-10 #3: Smart fallback uses real active offer ─────────────────────────

def build_fallback_action(merchant: dict, trigger: dict, merchant_id: str) -> dict:
    identity = merchant.get("identity", {})
    owner = identity.get("owner_first_name", "") or identity.get("name", "Merchant")
    # Use real active offer name (Top-10 spec requirement)
    offers = [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"]
    offer_str = offers[0] if offers else "your best offer"
    body = f"{owner}, your {offer_str} is live — want me to push it to nearby customers right now?"
    return {
        "conversation_id": f"conv_{merchant_id}_{trigger['id']}",
        "merchant_id": merchant_id,
        "customer_id": trigger.get("customer_id"),
        "send_as": "vera",
        "trigger_id": trigger["id"],
        "template_name": "vera_fallback_v1",
        "template_params": [owner, offer_str],
        "body": body[:320],
        "cta": "binary_yes_no",
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": "LLM fallback — using active offer + owner name",
    }


def extract_template_params(action: dict, merchant: dict) -> list[str]:
    identity = merchant.get("identity", {})
    owner = identity.get("owner_first_name", "") or identity.get("name", "")
    return [owner, action.get("body", "")[:100], action.get("cta", "")]
