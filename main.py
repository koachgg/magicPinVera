"""
main.py — FastAPI application entry point.
Implements the VERA bot contract (v1 endpoints).
"""
import time
import logging
import json
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError

from models import ContextBody, TickBody, ReplyBody
from store import upsert, get, count_by_scope, add_turn, get_turns, suppress, is_suppressed
from composer import compose, compose_reply

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] vera: %(message)s")
logger = logging.getLogger(__name__)

START_TIME = time.time()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    import os
    g_key = os.environ.get("GEMINI_API_KEY", "")
    logger.info("VERA Bot starting up... Key present: %s", bool(g_key))
    if g_key:
        logger.info("Key starts with: %s...", g_key[:6])
    yield
    # Shutdown logic
    logger.info("VERA Bot shutting down...")

app = FastAPI(title="VERA Merchant Assistant", version="1.0.0", lifespan=lifespan)

# ── GET|HEAD /v1/healthz ─────────────────────────────────────────────────────

@app.api_route("/v1/healthz", methods=["GET", "HEAD"])
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": count_by_scope(),
    }

# ── GET /v1/metadata ─────────────────────────────────────────────────────────

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "koachgg",
        "team_members": ["Belo Abhigyan"],
        "model": "gemini-2.0-flash",
        "approach": "dynamic prompt composer with trigger-kind dispatch, grounded in pushed context",
        "contact_email": "beloabhigyan@gmail.com",
        "bot_name": "VERA",
        "version": "1.0.0",
        "submitted_at": "2026-04-30T10:00:00Z",
        "features": ["3-tier-auto-reply", "intent-detection", "category-voice", "restraint-logic"]
    }

# ── POST /v1/context ──────────────────────────────────────────────────────────

@app.post("/v1/context")
async def post_context(body: ContextBody):
    success, current_v = upsert(
        scope=body.scope,
        context_id=body.context_id,
        payload=body.payload,
        version=body.version
    )
    if not success:
        raise HTTPException(status_code=409, detail={
            "accepted": False,
            "reason": "stale_version",
            "current_version": current_v
        })
    
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat()
    }

# ── POST /v1/tick ─────────────────────────────────────────────────────────────

@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    used_conversations = set()

    for trg_id in body.available_triggers:
        if len(actions) >= 20: break

        trigger = get("trigger", trg_id)
        if not trigger:
            logger.info("tick: trigger %s not in store, skipping", trg_id)
            continue
        
        # Ensure the trigger has its ID
        if "id" not in trigger:
            trigger["id"] = trg_id

        # Pre-check suppression
        suppression_key = trigger.get("suppression_key", "")
        if suppression_key and is_suppressed(suppression_key):
            logger.info("tick: suppressed key=%s, skipping %s", suppression_key, trg_id)
            continue

        # Compose the action (calls LLM)
        action = await compose(trigger, body.now)
        if not action: continue

        conv_id = action["conversation_id"]
        if conv_id in used_conversations: continue

        used_conversations.add(conv_id)

        # Success: Mark as suppressed ONLY after successful generation
        if suppression_key:
            suppress(suppression_key)

        merchant_id = action["merchant_id"]
        add_turn(conv_id, merchant_id, "vera", action["body"])
        actions.append(action)

    return {"actions": actions}

# ── POST /v1/reply ────────────────────────────────────────────────────────────

@app.post("/v1/reply")
async def reply(body: ReplyBody):
    history = get_turns(body.conversation_id)
    merchant_id = body.merchant_id
    if not merchant_id and history:
        merchant_id = history[0].get("merchant_id", "")

    add_turn(body.conversation_id, merchant_id, body.from_role or "merchant", body.message)

    end_key = f"conv_ended_{body.conversation_id}"
    if is_suppressed(end_key):
        return {"action": "end", "rationale": "Previously ended."}

    result = await compose_reply(
        conversation_id=body.conversation_id,
        merchant_id=merchant_id,
        customer_id=None,
        message=body.message,
        turn_number=body.turn_number
    )

    if result.get("action") == "end":
        suppress(end_key)
    
    if result.get("action") == "send":
        add_turn(body.conversation_id, merchant_id, "vera", result["body"])

    return result
