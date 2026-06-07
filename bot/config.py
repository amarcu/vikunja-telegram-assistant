"""Configuration from environment variables."""

import os
import sys
from zoneinfo import ZoneInfo


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing required environment variable: {name} (see .env.example)")
    return value


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = {
    int(uid) for uid in _require("TELEGRAM_ALLOWED_USER_IDS").replace(" ", "").split(",") if uid
}

VIKUNJA_URL = _require("VIKUNJA_URL").rstrip("/")
VIKUNJA_API_TOKEN = _require("VIKUNJA_API_TOKEN")
VIKUNJA_PUBLIC_URL = os.environ.get("VIKUNJA_PUBLIC_URL", VIKUNJA_URL).rstrip("/")

DEFAULT_PROJECT_ID = int(os.environ.get("DEFAULT_PROJECT_ID", "1"))

TIMEZONE_NAME = os.environ.get("TIMEZONE", "UTC")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

REMINDER_POLL_SECONDS = int(os.environ.get("REMINDER_POLL_SECONDS", "60"))

# Languages dateparser should expect, comma-separated (e.g. "en,ro").
DATEPARSER_LANGUAGES = [
    lang for lang in os.environ.get("DATEPARSER_LANGUAGES", "en").replace(" ", "").split(",") if lang
]

# Optional OpenAI-compatible LLM endpoint for free-form message parsing.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "").strip()
LLM_ENABLED = bool(LLM_BASE_URL and LLM_MODEL)

# Where the reminder poller remembers what it already sent.
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
