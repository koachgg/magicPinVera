"""
store.py — In-memory context store with idempotent upsert.
Holds: contexts, conversations, suppression keys.
Thread-safety note: Python GIL protects dict operations for single-process uvicorn.
"""
import time
from datetime import datetime, timezone
from typing import Optional

# (scope, context_id) -> {"version": int, "payload": dict, "stored_at": str}
_contexts: dict[tuple[str, str], dict] = {}

# conversation_id -> [{"role": str, "text": str, "ts": str}]
_conversations: dict[str, list] = {}

# conversation_id -> merchant_id  (so /v1/reply can look up merchant without body field)
_conv_to_merchant: dict[str, str] = {}

# suppressed suppression_keys this session
_suppressed: set[str] = set()


def upsert(scope: str, context_id: str, version: int, payload: dict) -> tuple[bool, int]:
    """
    Idempotent upsert.
    Returns (accepted: bool, current_version: int).
    - Same or lower version -> (False, current_version)  [409 stale_version]
    - Higher version        -> (True, version)            [200 accepted, replace atomically]
    """
    key = (scope, context_id)
    existing = _contexts.get(key)
    if existing is not None and existing["version"] >= version:
        return False, existing["version"]
    _contexts[key] = {
        "version": version,
        "payload": payload,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }
    return True, version


def get(scope: str, context_id: str) -> Optional[dict]:
    """Return payload dict or None if not found."""
    entry = _contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def get_version(scope: str, context_id: str) -> Optional[int]:
    """Return current stored version or None."""
    entry = _contexts.get((scope, context_id))
    return entry["version"] if entry else None


def get_all_of_scope(scope: str) -> dict[str, dict]:
    """Return {context_id: payload} for all entries with given scope."""
    return {cid: v["payload"] for (s, cid), v in _contexts.items() if s == scope}


def count_by_scope() -> dict[str, int]:
    """Return counts per scope. Judge validates these numbers."""
    counts: dict[str, int] = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in _contexts:
        if scope in counts:
            counts[scope] += 1
        else:
            counts[scope] = 1
    return counts


def add_turn(conversation_id: str, merchant_id: str, role: str, text: str) -> None:
    """Append a conversation turn. Roles: 'vera', 'merchant', 'customer'."""
    if conversation_id not in _conversations:
        _conversations[conversation_id] = []
    _conversations[conversation_id].append({
        "role": role,
        "text": text,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    if merchant_id:
        _conv_to_merchant[conversation_id] = merchant_id


def get_turns(conversation_id: str) -> list:
    """Return all turns for a conversation (oldest first)."""
    return _conversations.get(conversation_id, [])


def get_merchant_for_conv(conversation_id: str) -> Optional[str]:
    """Look up which merchant_id a conversation belongs to."""
    return _conv_to_merchant.get(conversation_id)


def conv_exists(conversation_id: str) -> bool:
    """True if this conversation_id has ever been used."""
    return conversation_id in _conversations


def suppress(key: str) -> None:
    """Mark a suppression_key as used for this session."""
    _suppressed.add(key)


def is_suppressed(key: str) -> bool:
    """Return True if this suppression_key has already been used."""
    return key in _suppressed
