"""Parse a free-form message into task fields — no LLM required.

Supported syntax (all optional, any order):
    buy milk tomorrow 6pm #personal !3
    └ title ─┘ └─ date ─┘ └project┘ └priority 1-5┘

Dates are found with dateparser's search_dates, so most natural English
works: "tomorrow 6pm", "friday", "in 2 hours", "june 21", "next week".
Other languages too — set DATEPARSER_LANGUAGES (e.g. "en,ro").
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from dateparser.search import search_dates

PRIORITY_RE = re.compile(r"(?:^|\s)!([1-5])\b")
PROJECT_RE = re.compile(r"(?:^|\s)#(\w[\w-]*)")

# A list item: a line led by a bullet (•, -, *, ·) or "1." / "1)" numbering.
BULLET_RE = re.compile(r"^\s*(?:[•▪◦·・\-\*]|\d+[.)])\s+(.+)$")

# search_dates is eager; only trust matches that look like actual date
# phrases (contain a letter or time/date separator), so "buy 2 milk"
# doesn't lose its "2".
_PLAUSIBLE_DATE_RE = re.compile(r"[a-zA-Z:/]")

# A match that names a clock time ("6pm", "18:00") beats a bare day word
# ("today"), so "to-do until 6pm" resolves to 18:00, not today-at-now.
_HAS_TIME_RE = re.compile(r"\d\s*(?:am|pm)\b|\d{1,2}:\d{2}", re.IGNORECASE)


@dataclass
class ParsedTask:
    title: str
    due_date: datetime | None = None
    priority: int | None = None
    project_hint: str | None = None
    matched_date_text: str | None = None


def parse_message(text: str, timezone_name: str, languages: list[str] | None = None) -> ParsedTask:
    result = ParsedTask(title=text.strip())
    working = text.strip()

    if match := PRIORITY_RE.search(working):
        result.priority = int(match.group(1))
        working = PRIORITY_RE.sub(" ", working, count=1)

    if match := PROJECT_RE.search(working):
        result.project_hint = match.group(1).lower()
        working = PROJECT_RE.sub(" ", working, count=1)

    # dateparser assumes ONE language per text, so query each language
    # separately and keep the longest plausible match across all of them
    # (e.g. ro:"mâine la 10:00" beats en:"10:00").
    candidates = []
    for language in languages or ["en"]:
        found = search_dates(
            working,
            languages=[language],
            settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE": timezone_name,
                "RETURN_AS_TIMEZONE_AWARE": True,
            },
        )
        candidates += [
            (matched, when)
            for matched, when in found or []
            if len(matched) > 2 and _PLAUSIBLE_DATE_RE.search(matched)
        ]
    if candidates:
        # Prefer a time-bearing match, then the longest, so "until 6pm" wins
        # over "today" and "mâine la 10:00" wins over a lone "10:00".
        matched_text, when = max(
            candidates, key=lambda c: (bool(_HAS_TIME_RE.search(c[0])), len(c[0]))
        )
        result.due_date = when
        result.matched_date_text = matched_text
        working = working.replace(matched_text, " ", 1)

    title = re.sub(r"\s+", " ", working).strip(" ,.-—")
    result.title = title or text.strip()
    return result


def parse_messages(
    text: str, timezone_name: str, languages: list[str] | None = None
) -> list[ParsedTask]:
    """Split a bulleted / numbered / multi-line list into one task per item.

    A header above the list (e.g. "tasks for today until 6pm:") contributes a
    shared due date / priority / project to every item that doesn't set its own.
    Falls back to a single task when there's no list.
    """
    header_lines: list[str] = []
    items: list[str] = []
    for line in text.splitlines():
        if match := BULLET_RE.match(line):
            items.append(match.group(1).strip())
        elif items:
            if line.strip():  # a trailing non-bullet line is its own item
                items.append(line.strip())
        else:
            header_lines.append(line)

    if len(items) < 2:
        return [parse_message(text, timezone_name, languages)]

    header = " ".join(header_lines).strip()
    shared = parse_message(header, timezone_name, languages) if header.strip(" :,.-—") else None

    results = []
    for item in items:
        task = parse_message(item, timezone_name, languages)
        if shared:
            if task.due_date is None:
                task.due_date = shared.due_date
            if task.priority is None:
                task.priority = shared.priority
            if task.project_hint is None:
                task.project_hint = shared.project_hint
        results.append(task)
    return results
