"""Optional LLM parsing via any OpenAI-compatible /chat/completions endpoint.

Used only when LLM_BASE_URL + LLM_MODEL are configured. Works with Ollama
(http://host:11434/v1), llama.cpp server, OpenRouter, OpenAI, etc.
Falls back to parsing.py on any failure — the bot never depends on the LLM.
"""

import json
import logging
import re
from datetime import datetime

import httpx

import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You extract one task from a chat message. Reply with ONLY a JSON object, no prose:
{"title": "...", "due_date": "... or null", "priority": 0, "project": "... or null"}

Rules:
- title: the task in the SAME language as the message — NEVER translate — with
  date/time/urgency words removed.
- due_date: ISO 8601 with timezone offset, resolved against the current local
  time given below; null if the message implies no date. A weekday name means
  the NEXT occurrence of that weekday. A bare hour like "ora 10" or "10am"
  means that time of day.
- priority: 0 unless urgency is expressed (1 low ... 5 do-now).
- project: best-matching name from the project list, else null.

Examples — these assume now = Monday 2026-06-08T09:00+03:00:
"trimite raportul joi la 18, urgent" -> {"title": "trimite raportul", "due_date": "2026-06-11T18:00:00+03:00", "priority": 4, "project": null}
"buy milk tomorrow 6pm" -> {"title": "buy milk", "due_date": "2026-06-09T18:00:00+03:00", "priority": 0, "project": "Personal"}
"call mom sometime next week" -> {"title": "call mom", "due_date": "2026-06-15T09:00:00+03:00", "priority": 0, "project": null}

Current local time: {now}. Timezone: {tz}.
Projects: {projects}.
"""

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


async def parse_with_llm(text: str, project_names: list[str]) -> dict | None:
    """Return {title, due_date: datetime|None, priority, project} or None on failure."""
    now = datetime.now(config.TIMEZONE)
    system = SYSTEM_PROMPT.replace("{now}", f"{now:%A} {now.isoformat(timespec='minutes')}")
    system = system.replace("{tz}", config.TIMEZONE_NAME)
    system = system.replace("{projects}", ", ".join(project_names) or "(none)")

    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"

    try:
        # Generous timeout: a local model may cold-load into VRAM on first call.
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            response = await client.post(
                f"{config.LLM_BASE_URL}/chat/completions",
                headers=headers,
                json={
                    "model": config.LLM_MODEL,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": text},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError) as exc:
        logger.warning("LLM call failed, falling back to date parser: %s", exc)
        return None

    # Tolerate reasoning models (<think> blocks) and code fences.
    content = THINK_RE.sub("", content)
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        data = json.loads(content)
        due = data.get("due_date")
        data["due_date"] = datetime.fromisoformat(due) if due else None
        data["title"] = (data.get("title") or "").strip()
        if not data["title"]:
            return None
        return data
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("LLM returned unparseable JSON, falling back: %s", exc)
        return None
