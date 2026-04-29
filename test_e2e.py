import requests
import json
import time

BASE = "http://127.0.0.1:8080"

def test():
    print("1. Loading Category...")
    r1 = requests.post(f"{BASE}/v1/context", json={
        "scope": "category", "context_id": "dentists", "version": 1,
        "delivered_at": "2026-04-30T10:00:00Z",
        "payload": {
            "slug": "dentists", "voice": {"tone": "peer_clinical"},
            "peer_stats": {"avg_ctr": 0.030},
            "digest": [{
                "id": "d_001", "title": "3-mo fluoride recall cuts caries 38%",
                "source": "JIDA 2026", "trial_n": 2100, "patient_segment": "high_risk"
            }]
        }
    })
    print(f"   Response: {r1.status_code} {r1.json()}")

    print("\n2. Loading Merchant...")
    r2 = requests.post(f"{BASE}/v1/context", json={
        "scope": "merchant", "context_id": "m_test", "version": 1,
        "delivered_at": "2026-04-30T10:00:00Z",
        "payload": {
            "merchant_id": "m_test", "category_slug": "dentists",
            "identity": {"name": "Test Clinic", "owner_first_name": "Meera"},
            "offers": [{"id": "o_001", "title": "Rs.299 Cleaning", "status": "active"}]
        }
    })
    print(f"   Response: {r2.status_code} {r2.json()}")

    print("\n3. Loading Trigger...")
    r3 = requests.post(f"{BASE}/v1/context", json={
        "scope": "trigger", "context_id": "trg_test", "version": 1,
        "delivered_at": "2026-04-30T10:00:00Z",
        "payload": {
            "id": "trg_test", "kind": "research_digest", "merchant_id": "m_test",
            "payload": {"category": "dentists", "top_item_id": "d_001"},
            "suppression_key": f"test_sup_{time.time()}" # Unique key for this test
        }
    })
    print(f"   Response: {r3.status_code} {r3.json()}")

    print("\n4. Calling Tick...")
    r4 = requests.post(f"{BASE}/v1/tick", json={
        "now": "2026-04-30T10:10:00Z", "available_triggers": ["trg_test"]
    })
    data4 = r4.json()
    print(f"   Response: {json.dumps(data4, indent=2)}")
    
    if not data4.get("actions"):
        print("!! FAILED: No action returned in Tick")
        return

    conv_id = data4["actions"][0]["conversation_id"]
    
    print(f"\n5. Calling Reply (Yes) for {conv_id}...")
    r5 = requests.post(f"{BASE}/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": "m_test",
        "from_role": "merchant", "message": "Yes please, draft it",
        "received_at": "2026-04-30T10:15:00Z", "turn_number": 2
    })
    print(f"   Response: {json.dumps(r5.json(), indent=2)}")

if __name__ == "__main__":
    test()
