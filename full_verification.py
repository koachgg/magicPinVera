import requests
import json
import os
import glob
import time

BASE_URL = "http://127.0.0.1:8080"
DATASET_DIR = "../dataset"
EXPANDED_DIR = "../expanded"

results = {
    "total": 0,
    "passed": 0,
    "failed": 0,
    "failures": [],
    "body_lengths": []
}

def load_context(scope, context_id, payload, version=None):
    if version is None:
        version = int(time.time())
    data = {
        "scope": scope,
        "context_id": context_id,
        "version": version,
        "delivered_at": "2026-04-30T10:00:00Z",
        "payload": payload
    }
    r = requests.post(f"{BASE_URL}/v1/context", json=data)
    if r.status_code != 200:
        print(f"Failed to load {scope} {context_id}: {r.status_code} {r.text}")

def run_step_b():
    print("Loading 5 categories...")
    for cat_file in glob.glob(os.path.join(DATASET_DIR, "categories", "*.json")):
        with open(cat_file, 'r', encoding='utf-8') as f:
            payload = json.load(f)
            slug = os.path.basename(cat_file).replace('.json', '')
            load_context("category", slug, payload)

def run_step_c():
    print("Loading 50 merchants...")
    for file in glob.glob(os.path.join(EXPANDED_DIR, "merchants", "*.json")):
        with open(file, 'r', encoding='utf-8') as f:
            m = json.load(f)
            load_context("merchant", m["merchant_id"], m)

def run_step_d():
    print("Loading 200 customers...")
    for file in glob.glob(os.path.join(EXPANDED_DIR, "customers", "*.json")):
        with open(file, 'r', encoding='utf-8') as f:
            c = json.load(f)
            load_context("customer", c["customer_id"], c)

def run_step_e():
    print("Loading 100 triggers...")
    for file in glob.glob(os.path.join(EXPANDED_DIR, "triggers", "*.json")):
        with open(file, 'r', encoding='utf-8') as f:
            t = json.load(f)
            load_context("trigger", t["id"], t)

def run_step_f():
    print("Checking healthz...")
    r = requests.get(f"{BASE_URL}/v1/healthz")
    data = r.json()
    expected = {"category": 5, "merchant": 50, "customer": 200, "trigger": 100}
    counts = data.get("contexts_loaded", {})
    for k, v in expected.items():
        if counts.get(k) != v:
            print(f"FATAL: Context counts off. Expected {v} for {k}, got {counts.get(k)}")
            exit(1)
    print("All contexts loaded successfully!")

def assert_test(condition, pair_id, fail_reason):
    results["total"] += 1
    if not condition:
        results["failed"] += 1
        results["failures"].append(f"{pair_id}: {fail_reason}")
        return False
    results["passed"] += 1
    return True

def get_all_merchants():
    merchants = {}
    for file in glob.glob(os.path.join(EXPANDED_DIR, "merchants", "*.json")):
        with open(file, 'r', encoding='utf-8') as f:
            m = json.load(f)
            merchants[m["merchant_id"]] = m
    return merchants

def get_all_triggers():
    triggers = []
    for file in glob.glob(os.path.join(EXPANDED_DIR, "triggers", "*.json")):
        with open(file, 'r', encoding='utf-8') as f:
            triggers.append(json.load(f))
    return triggers

def run_step_g():
    print("Running 30 canonical test pairs...")
    with open(os.path.join(EXPANDED_DIR, "test_pairs.json"), 'r', encoding='utf-8') as f:
        test_pairs = json.load(f)
        
    merchants = get_all_merchants()

    for pair in test_pairs.get("pairs", []):
        t_id = pair["trigger_id"]
        expect_restraint = pair.get("expect_restraint", False)
        
        time.sleep(4.1) # rate limit respect (15 req/min for Gemini)
        r = requests.post(f"{BASE_URL}/v1/tick", json={"now": "2026-04-30T10:10:00Z", "available_triggers": [t_id]})
        data = r.json()
        actions = data.get("actions", [])
        
        if expect_restraint:
            assert_test(len(actions) == 0, t_id, "Expected restraint but got actions")
        else:
            if not assert_test(len(actions) > 0, t_id, "Actions array empty"):
                continue
                
            action = actions[0]
            body = action.get("body", "")
            rationale = action.get("rationale", "")
            
            results["body_lengths"].append(len(body))
            
            assert_test(len(body) <= 320, t_id, f"Body too long: {len(body)} chars")
            assert_test("http" not in body, t_id, "Body contains URL")
            assert_test(bool(rationale), t_id, "Empty rationale")
            
            m_id = action.get("merchant_id")
            m_data = merchants.get(m_id, {})
            owner = str(m_data.get("identity", {}).get("owner_first_name", "")).lower()
            offers = [str(o["title"]).lower() for o in m_data.get("offers", [])]
            locality = str(m_data.get("identity", {}).get("locality", "")).lower()
            
            body_lower = body.lower()
            has_fact = (owner in body_lower) or any(o in body_lower for o in offers) or (locality in body_lower)
            assert_test(has_fact, t_id, "Body missing context facts")

def run_step_h():
    print("Running cross-category tick test...")
    triggers = get_all_triggers()
    merchants = get_all_merchants()
        
    cats_to_test = {
        "dentists": "research_digest",
        "salons": "bridal_followup",
        "restaurants": "ipl_match_today",
        "gyms": "perf_dip", # Changed from seasonal_dip because generator uses perf_dip
        "pharmacies": "chronic_refill_due"
    }
    
    tested = set()
    for t in triggers:
        m_id = t["merchant_id"]
        cat = merchants[m_id]["category_slug"]
        kind = t["kind"]
        if cat in cats_to_test and cats_to_test[cat] == kind and cat not in tested:
            time.sleep(4.1) # Rate limit respect
            r = requests.post(f"{BASE_URL}/v1/tick", json={"now": "2026-04-30T10:10:00Z", "available_triggers": [t["id"]]})
            actions = r.json().get("actions", [])
            
            if kind == "ipl_match_today" and len(actions) == 0:
                assert_test(True, f"CrossCat-{cat}", "Passed via restraint")
                tested.add(cat)
                continue
                
            if len(actions) > 0:
                body = actions[0].get("body", "").lower()
                
                if cat == "dentists":
                    assert_test("—" in body or "patient" in body or "dr." in body or "rs." in body, f"CrossCat-{cat}", "Missing clinical tone/citation")
                elif cat == "salons":
                    assert_test(any(word in body for word in ["book", "prep", "ready", "glow", "bridal", "wedding", "appointment", "offer"]), f"CrossCat-{cat}", "Missing occasion language")
                elif cat == "restaurants":
                    assert_test(any(word in body for word in ["ipl", "match", "food", "order"]), f"CrossCat-{cat}", "Missing food/event language")
                elif cat == "gyms":
                    has_num = any(char.isdigit() for char in body)
                    assert_test(has_num, f"CrossCat-{cat}", "Missing numbers/metrics")
                elif cat == "pharmacies":
                    assert_test(any(word in body for word in ["refill", "medicine", "rx", "pill", "due", "health"]), f"CrossCat-{cat}", "Missing medical language")
                tested.add(cat)

def run_step_i():
    print("Running reply escalation test...")
    merchants_list = list(get_all_merchants().values())
        
    cats = ["dentists", "salons", "gyms"]
    for cat in cats:
        m_id = next((m["merchant_id"] for m in merchants_list if m["category_slug"] == cat), None)
        if not m_id: continue
        c_id = f"auto_{cat}"
        
        # Turn 1
        r1 = requests.post(f"{BASE_URL}/v1/reply", json={"conversation_id": c_id, "merchant_id": m_id, "from_role": "merchant", "message": "Thank you for contacting us", "received_at": "2026-04-30T10:00:00Z", "turn_number": 1}).json()
        assert_test(r1.get("action") == "send", f"Escalation-{cat}-1", "Expected send")
        
        # Turn 2
        r2 = requests.post(f"{BASE_URL}/v1/reply", json={"conversation_id": c_id, "merchant_id": m_id, "from_role": "merchant", "message": "Thank you for contacting us", "received_at": "2026-04-30T10:01:00Z", "turn_number": 2}).json()
        assert_test(r2.get("action") == "wait" and r2.get("wait_seconds") == 86400, f"Escalation-{cat}-2", "Expected wait 86400")
        
        # Turn 3
        r3 = requests.post(f"{BASE_URL}/v1/reply", json={"conversation_id": c_id, "merchant_id": m_id, "from_role": "merchant", "message": "band karo", "received_at": "2026-04-30T10:02:00Z", "turn_number": 3}).json()
        assert_test(r3.get("action") == "end", f"Escalation-{cat}-3", "Expected end on opt-out")

def print_summary():
    print("\n--- FINAL SUMMARY ---")
    print(f"Total tests run: {results['total']}")
    print(f"Passed: {results['passed']}")
    print(f"Failed: {results['failed']}")
    if results['failures']:
        print("Failed tests:")
        for f in results['failures']:
            print(f"  - {f}")
            
    lengths = results["body_lengths"]
    if lengths:
        print(f"Average body length: {sum(lengths)//len(lengths)} chars")
        print(f"Shortest body: {min(lengths)} chars")
        print(f"Longest body: {max(lengths)} chars")
    
    over_320 = any(l > 320 for l in lengths)
    print(f"Any body over 320 chars: {'YES' if over_320 else 'NO'}")
    
    empty_rationale = any("Empty rationale" in f for f in results["failures"])
    print(f"Any empty rationale: {'YES' if empty_rationale else 'NO'}")

if __name__ == "__main__":
    run_step_b()
    run_step_c()
    run_step_d()
    run_step_e()
    run_step_f()
    run_step_g()
    run_step_h()
    run_step_i()
    print_summary()
