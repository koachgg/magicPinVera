"""
main.py — FastAPI application with all 5 endpoints.
Implements spec Section 3 & 7 exactly.
"""
import time
import logging
from datetime import datetime, timezone

# Load .env for local development (no-op in production)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import store
from store import (
    upsert,
    get,
    count_by_scope,
    add_turn,
    suppress,
    is_suppressed,
    get_merchant_for_conv,
)
from composer import compose, compose_reply
from models import ContextBody, TickBody, ReplyBody

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vera")

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="VERA — Merchant Growth Assistant",
    description="magicpin AI bot: dynamic WhatsApp message composer",
    version="1.0.0",
)

START_TIME = time.time()

@app.on_event("startup")
async def startup_event():
    import os
    import google.generativeai as genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        try:
            genai.configure(api_key=api_key)
            print("\n--- AVAILABLE GEMINI MODELS ---")
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    print(f" - {m.name}")
            print("-------------------------------\n")
        except Exception as e:
            print(f"Error listing models: {e}")
    else:
        print("GEMINI_API_KEY not found in environment.")


# ── Exception handler ─────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": str(exc)[:200]},
    )


# ── GET /v1/healthz ───────────────────────────────────────────────────────────
@app.get("/v1/healthz")
async def healthz():
    """
    Returns server status and LIVE context counts.
    Judge validates contexts_loaded matches exactly what was pushed.
    """
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": count_by_scope(),
    }


# ── GET /v1/metadata ─────────────────────────────────────────────────────────
@app.get("/v1/metadata")
async def metadata():
    """Static team + model metadata."""
    return {
        "team_name": "vera-bot",
        "team_members": ["Abhigyan"],
        "model": "gemini-flash-latest",
        "approach": (
            "Dynamic prompt composer with trigger-kind dispatch, "
            "grounded in pushed context only. "
            "Gemini 2.0 Flash primary + Groq llama-3.3-70b fallback."
        ),
        "contact_email": "beloabhigyan@gmail.com",
        "version": "1.0.0",
        "submitted_at": "2026-04-29T10:00:00Z",
    }


# ── POST /v1/context ─────────────────────────────────────────────────────────
@app.post("/v1/context")
async def push_context(body: ContextBody):
    """
    Store context with idempotency:
    - Same or lower version → 409 stale_version (do NOT overwrite)
    - Higher version → 200 accepted (replace atomically)
    """
    accepted, current_version = upsert(
        scope=body.scope,
        context_id=body.context_id,
        version=body.version,
        payload=body.payload,
    )

    if not accepted:
        logger.info(
            "context rejected: scope=%s id=%s sent_v=%d stored_v=%d",
            body.scope, body.context_id, body.version, current_version,
        )
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": current_version,
            },
        )

    stored_at = datetime.now(timezone.utc).isoformat()
    ack_id = f"ack_{body.context_id}_v{body.version}"
    logger.info("context stored: scope=%s id=%s v=%d", body.scope, body.context_id, body.version)

    return {
        "accepted": True,
        "ack_id": ack_id,
        "stored_at": stored_at,
    }


# ── POST /v1/tick ─────────────────────────────────────────────────────────────
@app.post("/v1/tick")
async def tick(body: TickBody):
    """
    Process available triggers, compose messages, return actions.
    Rules:
    - Max 20 actions per tick
    - One action per conversation_id
    - Skip suppressed suppression_keys
    - Skip triggers not in store (context not pushed yet)
    - After action: mark suppression_key used, add turn to conversation
    """
    actions: list[dict] = []
    used_conversations: set[str] = set()

    logger.info("tick: now=%s triggers=%d", body.now, len(body.available_triggers))

    for trg_id in body.available_triggers:
        if len(actions) >= 20:
            logger.info("tick: reached max 20 actions, stopping")
            break

        # Load trigger from store
        trigger = get("trigger", trg_id)
        if not trigger:
            logger.info("tick: trigger %s not in store, skipping", trg_id)
            continue

        # Attach the trigger's id field to the payload for compose()
        # (store strips the envelope, so we re-inject it)
        if "id" not in trigger:
            trigger = {**trigger, "id": trg_id}

        # Pre-check suppression (compose() also checks, but this is faster)
        suppression_key = trigger.get("suppression_key", "")
        if suppression_key and is_suppressed(suppression_key):
            logger.info("tick: suppressed key=%s, skipping %s", suppression_key, trg_id)
            continue

        # Compose the action (calls LLM)
        action = await compose(trigger, body.now)
        if not action:
            logger.info("tick: compose returned None for trigger %s", trg_id)
            continue

        # Enforce unique conversation_id per tick
        conv_id = action["conversation_id"]
        if conv_id in used_conversations:
            logger.info("tick: duplicate conv_id %s, skipping", conv_id)
            continue

        # Mark resources as used
        used_conversations.add(conv_id)
        if suppression_key:
            suppress(suppression_key)

        # Record the sent message as a conversation turn
        merchant_id = action["merchant_id"]
        add_turn(conv_id, merchant_id, "vera", action["body"])

        actions.append(action)
        logger.info(
            "tick: action added conv=%s merchant=%s trigger=%s body_len=%d",
            conv_id, merchant_id, trg_id, len(action["body"]),
        )

    logger.info("tick: returning %d actions", len(actions))
    return {"actions": actions}


# ── POST /v1/reply ────────────────────────────────────────────────────────────
@app.post("/v1/reply")
async def reply(body: ReplyBody):
    """
    Handle merchant reply to an ongoing conversation.
    - Look up merchant_id from conversation store if not in request body
    - Add merchant turn to conversation
    - Compose reply (with auto-reply/opt-out/intent detection)
    - Add vera turn if action == "send"
    - Suppress conversation if action == "end"
    """
    # Resolve merchant_id (may not be in request body)
    merchant_id = body.merchant_id or get_merchant_for_conv(body.conversation_id)
    if not merchant_id:
        logger.warning("reply: no merchant_id for conv=%s", body.conversation_id)
        merchant_id = ""

    # Record the incoming merchant message
    add_turn(
        conversation_id=body.conversation_id,
        merchant_id=merchant_id,
        role=body.from_role or "merchant",
        text=body.message,
    )

    logger.info(
        "reply: conv=%s merchant=%s turn=%d from=%s",
        body.conversation_id, merchant_id, body.turn_number, body.from_role,
    )

    # Check if this conversation was already ended/suppressed
    end_key = f"conv_ended_{body.conversation_id}"
    if is_suppressed(end_key):
        return {
            "action": "end",
            "rationale": "Conversation previously ended. Not re-engaging.",
        }

    # Compose the reply
    result = await compose_reply(
        conversation_id=body.conversation_id,
        merchant_id=merchant_id,
        customer_id=body.customer_id,
        message=body.message,
        turn_number=body.turn_number,
    )

    # Record vera's reply turn
    if result.get("action") == "send" and result.get("body"):
        add_turn(body.conversation_id, merchant_id, "vera", result["body"])

    # Suppress future re-engagement if ended
    if result.get("action") == "end":
        suppress(end_key)
        logger.info("reply: conversation %s ended and suppressed", body.conversation_id)

    logger.info(
        "reply: conv=%s action=%s",
        body.conversation_id, result.get("action"),
    )
    return result


# ── Root redirect for health checks ─────────────────────────────────────────
@app.get("/")
async def root():
    return {"service": "vera-bot", "status": "ok", "healthz": "/v1/healthz"}
