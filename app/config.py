import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

# The one model token you need for Gemini Live lives in GOOGLE_API_KEY (.env).
LIVE_MODEL = os.getenv("LIVE_MODEL", "gemini-3.1-flash-live-preview")

def _normalise_db_url(url: str) -> str:
    """Heroku Postgres hands out `postgres://`, a scheme SQLAlchemy 2.0 has no
    dialect for (it raises "Can't load plugin: sqlalchemy.dialects:postgres").
    Fix it once, here, so every consumer gets a usable URL."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


DATABASE_URL = _normalise_db_url(
    os.getenv("DATABASE_URL", "postgresql://dental:dental@localhost:5432/dental"))


def _async_db_url(url: str) -> str:
    """Sync URL -> asyncpg URL for ADK's DatabaseSessionService.

    asyncpg does not accept libpq's `sslmode` query param (psycopg2 does), so it
    is translated to asyncpg's `ssl`. Managed Postgres (Heroku and friends)
    requires TLS; local docker Postgres does not, hence the host check.
    """
    parts = urlsplit(url
                     .replace("postgresql+psycopg2://", "postgresql+asyncpg://")
                     .replace("postgresql://", "postgresql+asyncpg://"))
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "sslmode"]
    if parts.hostname not in ("localhost", "127.0.0.1") and not any(
            k == "ssl" for k, _ in query):
        query.append(("ssl", "require"))
    return urlunsplit(parts._replace(query=urlencode(query)))


# ADK's DatabaseSessionService uses create_async_engine, so it needs an ASYNC
# driver. Our own code (models/PMS/seed) stays on sync psycopg2.
SESSION_DB_URL = os.getenv("SESSION_DB_URL") or _async_db_url(DATABASE_URL)
PMS_BASE_URL = os.getenv("PMS_BASE_URL", "http://localhost:8000/pms/v1")
CLINIC_CONFIG = os.getenv("CLINIC_CONFIG", "clinics/riverside.yaml")

# Live audio formats.
INPUT_SAMPLE_RATE = 16000    # Gemini Live input
OUTPUT_SAMPLE_RATE = 24000   # Gemini Live output
TWILIO_SAMPLE_RATE = 8000    # Twilio Media Streams (mu-law)

APP_NAME = "dental-receptionist"


def load_clinic(path: str | None = None) -> dict:
    """Load a clinic config YAML. This is the per-tenant data the agent reads."""
    cfg_path = ROOT / (path or CLINIC_CONFIG)
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
