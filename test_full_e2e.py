"""
test_full_e2e.py - Full spec verification. Run with uvicorn on port 8080.
"""
import requests, json, time

BASE = "http://127.0.0.1:8080"
results = []
V = int(time.time())  # Unique version per run — avoids stale_version conflicts

def check(label, actual, ok, expected_str=""):
    status = "[PASS]" if ok else "[FAIL]"
    print(f"  {status}  {label}")
    if not ok:
        print(f"         Expected: {expected_str}")
        print(f"         Got:      {json.dumps(actual)[:200]}")
    results.append(ok)

def post(path, body):
    r = requests.post(f"{BASE}{path}", json=body)
    try: return r.status_code, r.json()
    except: return r.status_code, {}

def get_req(path):
    r = requests.get(f"{BASE}{path}")
    return r.status_code, r.json()

# STEP 0: Healthz before context
print("\n=== STEP 0: Healthz (empty) ===")
s, d = get_req("/v1/healthz")
check("status=200", d, s == 200)
check("status=ok", d, d.get("status") == "ok")
check("all contexts=0", d, all(v == 0 for v in d.get("contexts_loaded", {}).values()), "all 0")

# STEP 0b: Metadata
print("\n=== STEP 0b: Metadata ===")
s, d = get_req("/v1/metadata")
check("status=200", d, s == 200)
check("team_name=koachgg", d, d.get("team_name") == "koachgg")
check("contact_email present", d, "@" in d.get("contact_email", ""))

# STEP 1: Category with digest
print("\n=== STEP 1: Load Category ===")
s, d = post("/v1/context", {
    "scope": "category", "context_id": "dentists", "version": 1,
    "delivered_at": "2026-04-30T10:00:00Z",
    "payload": {
        "slug": "dentists", "voice": {"tone": "peer_clinical"},
        "peer_stats": {"avg_ctr": 0.030},
        "digest": [{"id": "d_jida_001", "title": "3-mo fluoride recall cuts caries 38%",
                    "source": "JIDA Oct 2026, p.14", "trial_n": 2100, "patient_segment": "high_risk_adults"}]
    }
})
check("status=200", d, s == 200)
check("accepted=True", d, d.get("accepted") is True)
check("ack_id present", d, "ack_id" in d)

# STEP 1b: Idempotency
print("\n=== STEP 1b: Idempotency (same version) ===")
s, d = post("/v1/context", {"scope": "category", "context_id": "dentists", "version": 1,
                             "delivered_at": "2026-04-30T10:00:00Z", "payload": {}})
check("status=409", d, s == 409, "409")

# STEP 2: Full Merchant
print("\n=== STEP 2: Load Merchant ===")
s, d = post("/v1/context", {
    "scope": "merchant", "context_id": "m_001", "version": 1,
    "delivered_at": "2026-04-30T10:00:00Z",
    "payload": {
        "merchant_id": "m_001", "category_slug": "dentists",
        "identity": {"name": "Dr. Meera Dental Clinic", "owner_first_name": "Meera", "city": "Delhi"},
        "performance": {"views": 2410, "calls": 18, "ctr": 0.021, "delta_7d": {"views_pct": 0.18}},
        "offers": [{"id": "o_001", "title": "Dental Cleaning @ Rs.299", "status": "active"}],
        "customer_aggregate": {"lapsed_180d_plus": 78, "high_risk_adult_count": 124},
        "signals": ["ctr_below_peer_median", "high_risk_adult_cohort"]
    }
})
check("accepted=True", d, d.get("accepted") is True)

# STEP 3: Trigger
print("\n=== STEP 3: Load Research Trigger ===")
sup_key = f"research:dentists:2026-W17:{int(time.time())}"
s, d = post("/v1/context", {
    "scope": "trigger", "context_id": "trg_001", "version": 1,
    "delivered_at": "2026-04-30T10:00:00Z",
    "payload": {
        "id": "trg_001", "kind": "research_digest", "merchant_id": "m_001",
        "customer_id": None,
        "payload": {"category": "dentists", "top_item_id": "d_jida_001"},
        "urgency": 2, "suppression_key": sup_key, "expires_at": "2026-05-06T00:00:00Z"
    }
})
check("accepted=True", d, d.get("accepted") is True)

# STEP 4: Tick
print("\n=== STEP 4: Tick (Research Digest) ===")
s, d = post("/v1/tick", {"now": "2026-04-30T10:10:00Z", "available_triggers": ["trg_001"]})
check("status=200", d, s == 200)
check("has actions", d, len(d.get("actions", [])) > 0, "non-empty list")

conv_id = None
if d.get("actions"):
    a = d["actions"][0]
    conv_id = a.get("conversation_id")
    body_len = len(a.get("body", ""))
    check("body<=320 chars", a, body_len <= 320, "<=320")
    check("merchant_id=m_001", a, a.get("merchant_id") == "m_001")
    check("valid cta", a, a.get("cta") in ["open_ended", "binary_yes_no", "none"])
    check("suppression_key set", a, bool(a.get("suppression_key")))
    check("rationale present", a, len(a.get("rationale", "")) > 0)
    print(f"\n  Body ({body_len} chars): {a.get('body','')[:150]}...")
    print(f"  conv_id: {conv_id}")

# STEP 5: Reply YES (Action Mode)
print("\n=== STEP 5: Reply - Yes (Action Mode) ===")
if conv_id:
    s, d = post("/v1/reply", {
        "conversation_id": conv_id, "merchant_id": "m_001",
        "from_role": "merchant", "message": "Yes please, draft the patient WhatsApp",
        "received_at": "2026-04-30T10:15:00Z", "turn_number": 2
    })
    check("action=send", d, d.get("action") == "send")
    check("body<=320 chars", d, len(d.get("body","")) <= 320)
    bad = any(q in d.get("body","").lower() for q in ["just to confirm","can you tell me","what would you like"])
    check("no qualifying questions", d, not bad, "no 'just to confirm' etc.")
    print(f"  Body: {d.get('body','')[:150]}")
else:
    print("  SKIPPED (tick returned no actions)")
    results.append(False)

# STEP 6: Auto-Reply escalation
print("\n=== STEP 6a: Auto-Reply Tier 1 ===")
s, d = post("/v1/reply", {"conversation_id": "auto_01", "merchant_id": "m_001",
    "from_role": "merchant", "message": "Thank you for contacting us. Our team will respond shortly.",
    "received_at": "2026-04-30T10:20:00Z", "turn_number": 1})
check("action=send", d, d.get("action") == "send")

print("\n=== STEP 6b: Auto-Reply Tier 2 ===")
s, d = post("/v1/reply", {"conversation_id": "auto_01", "merchant_id": "m_001",
    "from_role": "merchant", "message": "Thank you for contacting us. Our team will respond shortly.",
    "received_at": "2026-04-30T10:21:00Z", "turn_number": 2})
check("action=wait", d, d.get("action") == "wait")
check("wait_seconds=86400", d, d.get("wait_seconds") == 86400)

print("\n=== STEP 6c: Auto-Reply Tier 3 ===")
s, d = post("/v1/reply", {"conversation_id": "auto_01", "merchant_id": "m_001",
    "from_role": "merchant", "message": "Thank you for contacting us. Our team will respond shortly.",
    "received_at": "2026-04-30T10:22:00Z", "turn_number": 3})
check("action=end", d, d.get("action") == "end")

# STEP 7: Opt-out
print("\n=== STEP 7: Opt-Out (band karo) ===")
s, d = post("/v1/reply", {"conversation_id": "optout_01", "merchant_id": "m_001",
    "from_role": "merchant", "message": "band karo yeh sab",
    "received_at": "2026-04-30T10:25:00Z", "turn_number": 1})
check("action=end", d, d.get("action") == "end")

# STEP 8: IPL + Dentist = restraint
print("\n=== STEP 8: IPL Trigger (Dentist - Restraint) ===")
post("/v1/context", {"scope": "trigger", "context_id": "trg_ipl", "version": 1,
    "delivered_at": "2026-04-30T10:25:00Z",
    "payload": {"id": "trg_ipl", "kind": "ipl_match_today", "merchant_id": "m_001",
                "customer_id": None, "payload": {}, "urgency": 1,
                "suppression_key": f"ipl:m_001:{int(time.time())}", "expires_at": "2026-04-30T23:00:00Z"}})
s, d = post("/v1/tick", {"now": "2026-04-30T10:25:00Z", "available_triggers": ["trg_ipl"]})
check("actions=[] (restraint applied)", d, d.get("actions") == [])

# STEP 9: Final healthz - context counts
print("\n=== STEP 9: Healthz (after load) ===")
s, d = get_req("/v1/healthz")
check("category>=1", d, d["contexts_loaded"]["category"] >= 1)
check("merchant>=1", d, d["contexts_loaded"]["merchant"] >= 1)
check("trigger>=2", d, d["contexts_loaded"]["trigger"] >= 2)

# SUMMARY
print(f"\n{'='*52}")
total = len(results)
passed = sum(results)
print(f"RESULTS: {passed}/{total} passed")
if passed == total:
    print("ALL GREEN -- SAFE TO PUSH TO PROD")
else:
    print(f"FAILED: {total-passed} checks -- DO NOT PUSH YET")
print('='*52)
