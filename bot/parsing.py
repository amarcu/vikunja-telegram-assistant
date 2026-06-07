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

# search_dates is eager; only trust matches that look like actual date
# phrases (contain a letter or time/date separator), so "buy 2 milk"
# doesn't lose its "2".
_PLAUSIBLE_DATE_RE = re.compile(r"[a-zA-Z:/]")


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
        matched_text, when = max(candidates, key=lambda c: len(c[0]))
        result.due_date = when
        result.matched_date_text = matched_text
        working = working.replace(matched_text, " ", 1)

    title = re.sub(r"\s+", " ", working).strip(" ,.-—")
    result.title = title or text.strip()
    return result
