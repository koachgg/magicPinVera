"""
models.py — Pydantic request/response schemas for all 5 endpoints.
Exact contract from spec Section 4.
"""
from pydantic import BaseModel
from typing import Any, Optional, Literal


# ── /v1/context ──────────────────────────────────────────────────────────────

class ContextBody(BaseModel):
    scope: Literal["category", "merchant", "customer", "trigger"]
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class ContextAccepted(BaseModel):
    accepted: bool
    ack_id: str
    stored_at: str


class ContextRejected(BaseModel):
    accepted: bool
    reason: str
    current_version: int


# ── /v1/tick ─────────────────────────────────────────────────────────────────

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class TickAction(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    send_as: str
    trigger_id: str
    template_name: str
    template_params: list[str]
    body: str
    cta: str
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: list[dict]


# ── /v1/reply ─────────────────────────────────────────────────────────────────

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ── /v1/healthz ──────────────────────────────────────────────────────────────

class HealthzResponse(BaseModel):
    status: str
    uptime_seconds: int
    contexts_loaded: dict[str, int]


# ── /v1/metadata ─────────────────────────────────────────────────────────────

class MetadataResponse(BaseModel):
    team_name: str
    team_members: list[str]
    model: str
    approach: str
    contact_email: str
    version: str
    submitted_at: str
