# VERA — Merchant Growth Assistant Bot

Stateful FastAPI bot that composes sharp, merchant-specific WhatsApp messages from pushed context.
Part of the magicpin AI challenge.

---

## LLM Stack
- **Primary**: Gemini 2.0 Flash (via Google AI REST API)
- **Fallback**: Groq `llama-3.3-70b-versatile` (OpenAI-compatible endpoint)

---

## Project Structure

```
vera-bot/
├── main.py          ← FastAPI app, 5 endpoints
├── store.py         ← In-memory context store, idempotent upsert
├── composer.py      ← LLM calls (Gemini + Groq), compose(), compose_reply()
├── prompts.py       ← Prompt builders, CATEGORY_VOICE, TRIGGER_KIND_MAP
├── models.py        ← Pydantic schemas
├── requirements.txt
├── Dockerfile
└── README.md
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Google AI Studio API key |
| `GROQ_API_KEY` | No | Groq API key (fallback LLM) |

---

## Local Development

```bash
# 1. Clone and enter directory
cd vera-bot

# 2. Create .env file
echo "GEMINI_API_KEY=your_gemini_key_here" > .env
echo "GROQ_API_KEY=your_groq_key_here" >> .env

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# 5. Test endpoints
curl http://localhost:8080/v1/healthz
curl http://localhost:8080/v1/metadata
```

---

## Quick Smoke Test

```bash
# Push category context
curl -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "category",
    "context_id": "dentists",
    "version": 1,
    "payload": {
      "slug": "dentists",
      "display_name": "Dentists",
      "voice": {"tone": "peer_clinical"},
      "peer_stats": {"avg_rating": 4.4, "avg_reviews": 62, "avg_ctr": 0.030},
      "digest": [{"id": "d_2026W17_jida_fluoride", "kind": "research", "title": "3-mo fluoride recall cuts caries 38%", "source": "JIDA Oct 2026, p.14", "trial_n": 2100, "patient_segment": "high_risk_adults", "summary": "3-month recall intervals significantly outperform 6-month intervals"}]
    },
    "delivered_at": "2026-04-29T10:00:00Z"
  }'

# Push merchant context
curl -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "merchant",
    "context_id": "m_001_drmeera_dentist_delhi",
    "version": 1,
    "payload": {
      "merchant_id": "m_001_drmeera_dentist_delhi",
      "category_slug": "dentists",
      "identity": {"name": "Dr. Meera'\''s Dental Clinic", "city": "Delhi", "locality": "Lajpat Nagar", "owner_first_name": "Meera"},
      "subscription": {"status": "active", "plan": "Pro", "days_remaining": 82},
      "performance": {"window_days": 30, "views": 2410, "calls": 18, "ctr": 0.021, "leads": 9, "delta_7d": {"views_pct": 0.18, "calls_pct": -0.05}},
      "offers": [{"id": "o_meera_001", "title": "Dental Cleaning @ \u20b9299", "status": "active"}],
      "signals": ["ctr_below_peer_median", "high_risk_adult_cohort"],
      "customer_aggregate": {"total_unique_ytd": 540, "high_risk_adult_count": 124}
    },
    "delivered_at": "2026-04-29T10:00:00Z"
  }'

# Push trigger context
curl -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "trigger",
    "context_id": "trg_001_research_digest_dentists",
    "version": 1,
    "payload": {
      "id": "trg_001_research_digest_dentists",
      "kind": "research_digest",
      "merchant_id": "m_001_drmeera_dentist_delhi",
      "customer_id": null,
      "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
      "urgency": 2,
      "suppression_key": "research:dentists:2026-W17",
      "expires_at": "2026-05-03T00:00:00Z"
    },
    "delivered_at": "2026-04-29T10:00:00Z"
  }'

# Fire tick
curl -X POST http://localhost:8080/v1/tick \
  -H "Content-Type: application/json" \
  -d '{"now": "2026-04-29T10:35:00Z", "available_triggers": ["trg_001_research_digest_dentists"]}'

# Test reply
curl -X POST http://localhost:8080/v1/reply \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "conv_m_001_drmeera_dentist_delhi_trg_001_research_digest_dentists",
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "customer_id": null,
    "from_role": "merchant",
    "message": "Yes please send the abstract. Also draft the patient WhatsApp.",
    "received_at": "2026-04-29T10:42:00Z",
    "turn_number": 2
  }'
```

---

## Deploy to Render (10 minutes)

1. Push this repo to GitHub (public or private)
2. Go to [render.com](https://render.com) → **New** → **Web Service** → Connect your repo
3. Set **Environment**: `Docker`
4. Add environment variables:
   - `GEMINI_API_KEY` = your key
   - `GROQ_API_KEY` = your key (optional fallback)
5. Click **Deploy**
6. Wait 3-5 minutes → copy the `https://your-app.onrender.com` URL

### Keep Alive (Render Free Tier)
Render free tier sleeps after 15 min of inactivity.
- Use [UptimeRobot](https://uptimerobot.com) (free) to ping `/v1/healthz` every 5 minutes
- Or upgrade to Render paid ($7/mo) for zero cold starts

---

## API Contract

| Method | Path | Description |
|---|---|---|
| GET | `/v1/healthz` | Server status + context counts |
| GET | `/v1/metadata` | Team + model info |
| POST | `/v1/context` | Push context (idempotent, versioned) |
| POST | `/v1/tick` | Process triggers → compose messages |
| POST | `/v1/reply` | Handle merchant reply in conversation |

### Idempotency Rules (/v1/context)
- Same version re-posted → `409 stale_version` (no overwrite)
- Higher version → `200 accepted` (atomic replace)
- Lower version → `409 stale_version`

---

## Hard Constraints (Auto-Enforced)

- ✅ `body` ≤ 320 chars — `body[:320]` + truncation with `...`
- ✅ No URLs in body — regex stripping of `http*` and `www.*`
- ✅ `temperature=0` — deterministic output on both providers
- ✅ Suppression keys prevent duplicate sends
- ✅ Unique `conversation_id` per tick action
- ✅ `/v1/tick` responds within 10s — 7s Gemini + 6s Groq timeouts
- ✅ `rationale` always populated — required for judge scoring
