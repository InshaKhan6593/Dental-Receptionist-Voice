# AGENTS.md — Dental Receptionist

Context file for AI agents (and new humans) working on this repo.

## What this project is

An inbound **AI voice receptionist** for a dental practice. A caller phones in
(Twilio) or uses the browser mic test page, talks to a **Gemini Live** agent
driven by **Google ADK**, and the agent verifies patients, checks availability,
and books/reschedules/cancels appointments against a **mock Dentally-shaped PMS
API** backed by Postgres. Every call is logged to Postgres and traced to
**LangSmith** with the caller/agent audio attached as WAVs.

- Language: Python 3.11+ (single package, no monorepo)
- Web framework: FastAPI + uvicorn, WebSockets for audio streaming
- Agent: `google-adk` `LlmAgent` on `gemini-3.1-flash-live-preview` (BIDI audio)
- DB: Postgres 16 via Docker (SQLAlchemy sync `psycopg2` for app tables, `asyncpg` for ADK session persistence)
- Package manager: **uv** (`uv.lock` present, `.venv/` in repo root)
- Demo tenant: **Riverside Dental Practice** (`clinics/riverside.yaml`)

## How to run

There is **no separate frontend server**. The frontend is a static browser
voice-test client (`static/`) that the FastAPI backend serves at `/` — one
command runs both.

```powershell
# 1. Start Postgres (host port 5433 -> container 5432; 5432 is often taken on Windows)
docker compose up -d

# 2. Install deps (creates/updates .venv)
uv sync

# 3. Config: copy .env.example to .env and set GOOGLE_API_KEY (Gemini).
#    LANGSMITH_API_KEY is optional (voice traces). .env already exists locally.

# 4. Seed the mock PMS (once; prints known test patients for the demo)
uv run python -m scripts.seed_data

# 5. Run backend + frontend (single process)
uv run uvicorn app.main:app --reload --port 8000
```

Then open:

| URL | What |
|---|---|
| http://localhost:8000/ | Frontend — browser voice test client (click the button, talk) |
| http://localhost:8000/healthz | Health: clinic name + cached practitioner count |
| http://localhost:8000/pms/v1/practitioners | Mock Dentally PMS API |

Without uv, the equivalent is: activate `.venv` and run
`python -m scripts.seed_data` / `uvicorn app.main:app --reload --port 8000`.

### Ports & gotchas

- **Postgres is on host port 5433**, not 5432 (`docker-compose.yml` maps
  `5433:5432`). `.env` has `DATABASE_URL=postgresql://dental:dental@127.0.0.1:5433/dental`;
  `.env.example` shows 5432 — trust `.env` locally.
- `GOOGLE_API_KEY` is only needed the moment a live call starts; the server
  boots without it.
- `static/runtime-config.js` sets `window.VOICE_AGENT_ORIGIN = ""` — blank means
  same-origin (local dev). Only set it if the frontend is hosted separately
  (e.g. Netlify) from the backend (e.g. Heroku).
- Twilio is scaffolded but needs a purchased number + public URL (ngrok):
  point the number's Voice webhook at `POST https://<public-host>/twilio/voice`.
- `audioop` left the stdlib in Python 3.13; `audioop-lts` is a conditional
  dependency, already handled in `pyproject.toml`.
- There are no tests or linters configured yet.

## Architecture (the flow)

```
Caller ─PSTN─► Twilio ─Media Streams(μ-law 8k)─► FastAPI
Browser ─mic (PCM16 16k)────────────────────────►   │
                                                    ├─ audio bridge  μ-law8k ⇄ PCM16k/24k
                                                    ├─ ADK Runner ──BIDI──► Gemini Live
                                                    ├─ tools ──HTTP──► mock PMS (/pms/v1, same app)
                                                    ├─ resolver ──► in-memory reference cache
                                                    ├─ callbacks: verify-before-book guardrail
                                                    └─ sessions/state ──► Postgres
                              call end ──► CallLog row + LangSmith trace (WAVs attached)
```

Two data paths (deliberate design):

1. **Resolution (offline):** the model passes *names* ("Dr Hepburn",
   "check-up"); `app/cache.py` fuzzy-resolves them to practitioner IDs /
   Dentally reasons from an in-memory cache loaded at startup. The PMS is
   never hit per conversation turn for reference data.
2. **Live PMS:** verify / availability / book / reschedule / cancel go over
   HTTP to `PMS_BASE_URL`. Today that's the mock `/pms/v1` router in the same
   process; swapping to real Dentally later means changing only the base URL
   and token.

Rule: **names cross the model boundary, IDs stay server-side.** Tools read
process-wide singletons from `app/runtime.py` (CLINIC, CACHE, PMS) so the model
never sees clients or credentials.

## File map

| Path | What |
|---|---|
| `app/main.py` | FastAPI app: Twilio bridge (`/twilio/voice`, WS `/twilio/stream`), browser bridge (WS `/ws/{user_id}`), `/clinic` rename endpoint, `/healthz`, mounts `static/` at `/` **last** |
| `app/agent.py` | Builds the ADK `LlmAgent`; instruction is templated from the clinic YAML |
| `app/tools.py` | ADK tools: verify (returns usual dentist), availability (day/time/date filters, parallel per-day windows), get_more_slots (paged from state, no HTTP), book, reschedule, cancel, callback request, transfer |
| `app/callbacks.py` | ADK before/after tool callbacks: verify-before-book guardrail + safe fallback |
| `app/cache.py` | `ReferenceCache` + rapidfuzz name→ID resolver |
| `app/pms.py` | Mock Dentally REST API (`/pms/v1/*` router) **and** the `PMSClient` the tools use |
| `app/models.py` | SQLAlchemy models: Dentally-shaped PMS tables + app tables (call_logs, callback_requests) |
| `app/telephony.py` | μ-law 8k ⇄ PCM 16k/24k transcoding for the Twilio leg |
| `app/tracing.py` | LangSmith per-call `voice_call` runs, `@traced_tool` for PMS ops, `CallRecorder` WAVs |
| `app/config.py` | Env/config loading (`load_dotenv`), audio sample-rate constants, `load_clinic()` |
| `app/runtime.py` | Process-wide singletons: `CLINIC`, `CACHE`, `PMS` |
| `scripts/seed_data.py` | Faker (en_GB, seeded) dataset: ~12 clinicians, ~3000 patients, appointments + fixed known test patients |
| `clinics/riverside.yaml` | The only per-tenant file: hours, appointment types, verification fields, escalation, emergency script |
| `static/` | Frontend: `index.html`, `app.js` (mic capture/playback), AudioWorklet processors, `runtime-config.js` |

## Conventions & constraints

- **Multi-tenant via YAML:** everything clinic-specific (name, hours,
  appointment types, verification policy, escalation) lives in
  `clinics/*.yaml`, selected by `CLINIC_CONFIG`. New clinic = new YAML file.
  Never hardcode clinic details in Python.
- **Guardrails are enforced in code, not just prompt:** patient verification
  before booking is checked in the tools *and* in `before_tool` callback.
  Keep both when touching booking logic.
- **Audio formats are fixed:** Gemini Live input 16 kHz PCM, output 24 kHz PCM,
  Twilio 8 kHz μ-law. Constants live in `app/config.py`.
- **Sync vs async DB:** app models/PMS/seed use sync psycopg2; ADK's
  `DatabaseSessionService` needs the asyncpg URL (`SESSION_DB_URL` is derived
  automatically in `app/config.py`).
- Secrets live in `.env` (gitignored). Never commit keys or put them in code,
  docs, or clinic YAMLs (YAMLs reference token *env var names* only).
- Comments in this codebase explain the production rationale ("why"), not the
  mechanics — match that style.
