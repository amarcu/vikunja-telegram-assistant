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
You extract task data from a chat message. Reply with ONLY a JSON object:
{
  "title": "concise task title, imperative, no date words",
  "due_date": "ISO 8601 with timezone offset, or null if no date/time implied",
  "priority": 0,
  "project": "name of the best-matching project from the list, or null"
}
priority: 0 unless urgency is expressed (1 low ... 5 do-now).
The message may be in any language; keep the title in that language and
resolve relative dates ("mañana", "mâine", "tomorrow") against the current
local time below.
Current local time: {now}. Timezone: {tz}.
Existing projects: {projects}.
"""

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


async def parse_with_llm(text: str, project_names: list[str]) -> dict | None:
    """Return {title, due_date: datetime|None, priority, project} or None on failure."""
    system = SYSTEM_PROMPT.replace("{now}", datetime.now(config.TIMEZONE).isoformat())
    system = system.replace("{tz}", config.TIMEZONE_NAME)
    system = system.replace("{projects}", ", ".join(project_names) or "(none)")

    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
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
