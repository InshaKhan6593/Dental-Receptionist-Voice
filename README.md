# Dental Receptionist — production-grade AI voice agent

Inbound AI voice receptionist for a dental practice, built on **Google ADK +
Gemini Live**, with **Twilio** telephony, a **Dentally-shaped PMS** (mock now,
real later), **Postgres** session + message persistence, and **LangSmith**
per-call voice-recording traces.

> Demo clinic: **Riverside Dental Practice** (edit `clinics/riverside.yaml`, or
> `POST /clinic {"name": "..."}` to rebrand on the fly).

## Architecture

```
Caller ─PSTN─► Twilio ─Media Streams(μ-law 8k)─► FastAPI (this app)
                                                   ├─ audio bridge  μ-law8k ⇄ PCM16k/24k
                                                   ├─ ADK Runner ──BIDI──► Gemini Live
                                                   ├─ tools ──LIVE──► PMS API (mock Dentally)
                                                   ├─ resolver ──► reference cache (names→IDs)
                                                   ├─ callbacks (verify-before-book, fallback)
                                                   └─ sessions ──► Postgres (state + events)
                            call end ──► CallLog + LangSmith trace (caller/agent WAV attached)
```

Two data paths (the production trick):
- **Resolution (offline):** `resolve_clinician`/`resolve_appointment_type` → in-memory
  **reference cache** (practitioners, types). Never hits the PMS per turn.
- **Live PMS:** `verify` / `availability` / `book` / `reschedule` / `cancel` → HTTP to
  `PMS_BASE_URL` (mock `/pms/v1`, or real Dentally later — only the base URL changes).

## Layout

| Path | What |
|---|---|
| `app/models.py` | Dentally-shaped schema (patients, practitioners, appointment_reasons, appointments) + call logs |
| `app/pms.py` | Mock Dentally REST API (`/pms/v1/*`) **and** the `PMSClient` the tools call |
| `app/cache.py` | Reference cache + name→ID resolver (fuzzy) |
| `app/tools.py` | ADK tools: verify, availability, book, reschedule, cancel, callback, transfer |
| `app/callbacks.py` | ADK middleware: verify-before-book guardrail + safe-fallback |
| `app/agent.py` | ADK `LlmAgent`, prompt templated per clinic |
| `app/telephony.py` | μ-law ⇄ PCM transcoding |
| `app/tracing.py` | LangSmith call trace + voice-recording (WAV) attachments |
| `app/main.py` | FastAPI: Twilio + browser bridges, TwiML, session persistence |
| `scripts/seed_data.py` | Faker medium-scale UK dataset |
| `clinics/riverside.yaml` | The only per-tenant file |

## Run locally

```bash
# 1. Postgres
docker compose up -d

# 2. Deps (uv or pip)
uv sync            # or:  pip install -e .

# 3. Config — copy and fill in
cp .env.example .env
#   GOOGLE_API_KEY   = your Gemini API key   (the one model token you need)
#   LANGSMITH_API_KEY= your LangSmith key     (optional, for voice traces)

# 4. Seed the mock PMS (prints known test patients)
python -m scripts.seed_data

# 5. Run
uvicorn app.main:app --reload --port 8000
```

Health check: `GET http://localhost:8000/` → clinic name + cached practitioner count.
Mock PMS: e.g. `GET http://localhost:8000/pms/v1/practitioners`.

## Wiring Twilio (once you buy a number)

Point the number's **Voice webhook** at `POST https://<public-host>/twilio/voice`
(use ngrok locally, or your Heroku URL). The TwiML `<Connect><Stream>` bridges the
call into `/twilio/stream` automatically. No code change needed.

## LangSmith

With `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY`, every call creates a
`voice_call` run in project `dental-receptionist` with the **caller and agent audio
attached as WAVs**, plus transcript and disposition. Each PMS operation appears as a
`tool` run.

## Notes / next steps
- Twilio path is scaffolded; test it once you have a number + public URL.
- `audioop` is stdlib on Python ≤ 3.12; on 3.13+ add `audioop-lts`.
- Deploy: backend → Heroku (Standard dyno; Heroku Postgres for sessions), and point
  Twilio at the Heroku URL. Reference cache → move to a synced Postgres table / Redis
  at multi-clinic scale.
