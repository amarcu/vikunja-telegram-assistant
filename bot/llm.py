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
import parsing

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You extract one or MORE tasks from a chat message. Reply with ONLY a JSON array
of task objects, no prose:
[{"title": "...", "due_date": "... or null", "priority": 0, "project": "... or null", "repeat": "... or null"}]

Rules:
- Usually a message is ONE task -> return a single-element array.
- If the message is a LIST — bullet points (•, -, *), "1." numbering, or several
  short lines that are each a distinct action — return ONE object per list item.
- A shared header applies to EVERY item below it: a date/time, priority, project,
  or recurrence stated once (e.g. "tasks for today until 6pm:") attaches to all.
- title: the task in the SAME language as the message — NEVER translate — with
  date/time/urgency/recurrence words and list markers removed.
- due_date: ISO 8601 with timezone offset, resolved against the current local
  time given below; null if no date is implied. "until 6pm" / "by 6pm" /
  "to-do until 18" / "ora 18" means TODAY at that time. A weekday name means the
  NEXT occurrence of that weekday. A bare hour like "ora 10" or "10am" means that
  time of day. For a recurring task, due_date is the FIRST occurrence (e.g.
  "every day at 8pm" -> today or, if 8pm has passed, the next 8pm).
- priority: 0 unless urgency is expressed (1 low ... 5 do-now).
- project: best-matching name from the project list, else null.
- repeat: how often the task recurs, else null. Use one of: "daily", "weekly",
  "monthly", "hourly", or "every N days/weeks/hours/minutes". "X per day",
  "X a day", "each morning", "every day" -> "daily"; "every monday" -> "weekly".

Examples — these assume now = Monday 2026-06-08T09:00+03:00:
"trimite raportul joi la 18, urgent" -> [{"title": "trimite raportul", "due_date": "2026-06-11T18:00:00+03:00", "priority": 4, "project": null, "repeat": null}]
"buy milk tomorrow 6pm" -> [{"title": "buy milk", "due_date": "2026-06-09T18:00:00+03:00", "priority": 0, "project": "Personal", "repeat": null}]
"review 100 videos min per day for the dancer dataset" -> [{"title": "review 100 videos min for the dancer dataset", "due_date": null, "priority": 0, "project": null, "repeat": "daily"}]
"water the plants every 3 days" -> [{"title": "water the plants", "due_date": null, "priority": 0, "project": null, "repeat": "every 3 days"}]
"Add these tasks for today, to-do until 6PM:\\n• Update goats metadata\\n• Update AKCB mint page" -> [{"title": "Update goats metadata", "due_date": "2026-06-08T18:00:00+03:00", "priority": 0, "project": null, "repeat": null}, {"title": "Update AKCB mint page", "due_date": "2026-06-08T18:00:00+03:00", "priority": 0, "project": null, "repeat": null}]

Current local time: {now}. Timezone: {tz}.
Projects: {projects}.
"""

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


async def parse_with_llm(text: str, project_names: list[str]) -> list[dict] | None:
    """Return a list of {title, due_date: datetime|None, priority, project}.

    One element for a normal message, several for a bulleted/multi-line list.
    Returns None on any failure so the caller falls back to the date parser.
    """
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
        # Accept either a single object (legacy) or an array of tasks.
        items = data if isinstance(data, list) else [data]
        tasks = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            if not title:
                continue
            due = item.get("due_date")
            repeat_after, repeat_mode = parsing.repeat_to_seconds(item.get("repeat"))
            tasks.append(
                {
                    "title": title,
                    "due_date": datetime.fromisoformat(due) if due else None,
                    "priority": item.get("priority") or None,
                    "project": item.get("project"),
                    "repeat_after": repeat_after,
                    "repeat_mode": repeat_mode,
                }
            )
        return tasks or None
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("LLM returned unparseable JSON, falling back: %s", exc)
        return None
