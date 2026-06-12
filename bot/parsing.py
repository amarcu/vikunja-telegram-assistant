"""Parse a free-form message into task fields — no LLM required.

Supported syntax (all optional, any order):
    review 100 videos every day 8pm #dance !3
    └─ title ──────┘ └ recur ┘ └time┘ └project┘ └priority 1-5┘

Dates are found with dateparser's search_dates, so most natural English
works: "tomorrow 6pm", "friday", "in 2 hours", "june 21", "next week".
Other languages too — set DATEPARSER_LANGUAGES (e.g. "en,ro").

Recurrence ("every day", "per day", "weekly", "every 3 days", "every
monday") maps to Vikunja's native repeat_after/repeat_mode, so completing
the task auto-rolls it to the next occurrence.
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

# Bare unit words search_dates grabs as a time ("min" -> midnight). Only
# reject them standalone — "in 20 minutes" still parses (the match isn't bare).
_BARE_UNIT_WORDS = {"min", "mins", "minute", "minutes", "hr", "hrs", "sec", "secs", "max"}

# A match that names a clock time ("6pm", "18:00") beats a bare day word
# ("today"), so "to-do until 6pm" resolves to 18:00, not today-at-now.
_HAS_TIME_RE = re.compile(r"\d\s*(?:am|pm)\b|\d{1,2}:\d{2}", re.IGNORECASE)

# ── Recurrence ────────────────────────────────────────────────────────────────
# Maps to Vikunja: repeat_after is seconds; repeat_mode 0 = repeat relative to
# the due date (default), 1 = monthly (same day next month).
MINUTE, HOUR, DAY, WEEK = 60, 3600, 86400, 7 * 86400
REPEAT_MODE_DEFAULT, REPEAT_MODE_MONTH = 0, 1

_WEEKDAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_UNIT_SECONDS = {"minute": MINUTE, "min": MINUTE, "hour": HOUR, "day": DAY, "week": WEEK}

# "every 3 days", "every 2 weeks", "every 90 minutes", "every 6 months"
_EVERY_N_RE = re.compile(r"\bevery\s+(\d+)\s*(minute|min|hour|day|week|month)s?\b", re.IGNORECASE)
# "every monday" — weekly, but keep the weekday word so the date parser can
# anchor the next occurrence; we only strip the leading "every ".
_EVERY_WEEKDAY_RE = re.compile(rf"\bevery\s+(?={_WEEKDAYS})", re.IGNORECASE)
# Bare keyword forms. Order matters: try month/week/hour before day.
_MONTHLY_RE = re.compile(r"\b(?:every\s*month|each\s*month|monthly|per\s*month)\b", re.IGNORECASE)
_WEEKLY_RE = re.compile(r"\b(?:every\s*week|each\s*week|weekly|per\s*week)\b", re.IGNORECASE)
_HOURLY_RE = re.compile(r"\b(?:every\s*hour|each\s*hour|hourly|per\s*hour)\b", re.IGNORECASE)
_DAILY_RE = re.compile(r"\b(?:every\s*day|each\s*day|daily|per\s*day)\b|/\s*day\b", re.IGNORECASE)


def extract_recurrence(text: str) -> tuple[int | None, int, str]:
    """Pull a recurrence phrase out of `text`.

    Returns (repeat_after_seconds, repeat_mode, text_without_phrase). The
    phrase is removed early, before date parsing, so words like "day"/"week"
    in "every day" don't get eaten as a due date.
    """
    if match := _EVERY_N_RE.search(text):
        n = int(match.group(1))
        unit = match.group(2).lower()
        if unit == "month":
            # One month is calendar-accurate (month mode); N months falls back
            # to an approximate constant period since month mode is single-step.
            mode = REPEAT_MODE_MONTH if n == 1 else REPEAT_MODE_DEFAULT
            return n * 30 * DAY, mode, _cut(text, match)
        return n * _UNIT_SECONDS[unit], REPEAT_MODE_DEFAULT, _cut(text, match)

    if match := _EVERY_WEEKDAY_RE.search(text):
        # Drop only "every " — leave the weekday for the date parser to anchor.
        return WEEK, REPEAT_MODE_DEFAULT, text[: match.start()] + text[match.end() :]

    for regex, seconds, mode in (
        (_MONTHLY_RE, 30 * DAY, REPEAT_MODE_MONTH),
        (_WEEKLY_RE, WEEK, REPEAT_MODE_DEFAULT),
        (_HOURLY_RE, HOUR, REPEAT_MODE_DEFAULT),
        (_DAILY_RE, DAY, REPEAT_MODE_DEFAULT),
    ):
        if match := regex.search(text):
            return seconds, mode, _cut(text, match)

    return None, REPEAT_MODE_DEFAULT, text


def repeat_to_seconds(phrase: str | None) -> tuple[int | None, int]:
    """Map a recurrence word ("daily", "every 3 days", "monthly") to
    (repeat_after, repeat_mode). Used by the LLM path. None if not recurring.
    """
    if not phrase:
        return None, REPEAT_MODE_DEFAULT
    seconds, mode, _ = extract_recurrence(phrase)
    if seconds:
        return seconds, mode
    # Tolerate a bare "3 days" / "day" with no leading "every".
    seconds, mode, _ = extract_recurrence(f"every {phrase}")
    return (seconds, mode) if seconds else (None, REPEAT_MODE_DEFAULT)


def _cut(text: str, match: re.Match) -> str:
    """Remove a matched span, leaving a space so adjacent words don't fuse."""
    return text[: match.start()] + " " + text[match.end() :]


@dataclass
class ParsedTask:
    title: str
    due_date: datetime | None = None
    priority: int | None = None
    project_hint: str | None = None
    matched_date_text: str | None = None
    repeat_after: int | None = None
    repeat_mode: int = 0


def parse_message(text: str, timezone_name: str, languages: list[str] | None = None) -> ParsedTask:
    result = ParsedTask(title=text.strip())
    working = text.strip()

    # Strip the recurrence phrase first, so "every day"/"per day" don't leak
    # into the date parser as a bogus due date.
    result.repeat_after, result.repeat_mode, working = extract_recurrence(working)

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
            if len(matched) > 2
            and _PLAUSIBLE_DATE_RE.search(matched)
            and matched.strip().lower() not in _BARE_UNIT_WORDS
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
            if task.repeat_after is None:
                task.repeat_after = shared.repeat_after
                task.repeat_mode = shared.repeat_mode
        results.append(task)
    return results
