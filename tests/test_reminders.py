"""Tests for the schedule-driven recurring-reminder math. Run: pytest tests/

These cover current_occurrence(), which decides when a recurring task fires.
reminders.py imports config (which requires env vars), so set dummies first.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "1")
os.environ.setdefault("VIKUNJA_URL", "http://localhost:3456")
os.environ.setdefault("VIKUNJA_API_TOKEN", "test")

sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))

import reminders  # noqa: E402

DAY, WEEK = 86400, 7 * 86400


def task(due: str, repeat_after: int = 0, repeat_mode: int = 0) -> dict:
    return {"id": 1, "due_date": due, "repeat_after": repeat_after, "repeat_mode": repeat_mode}


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_daily_returns_today_slot():
    now = utc(2026, 6, 12, 10, 0)
    occ = reminders.current_occurrence(task("2026-06-01T09:00:00Z", DAY), now)
    assert occ == utc(2026, 6, 12, 9, 0)


def test_daily_before_slot_returns_yesterday():
    # 08:00, today's 09:00 slot hasn't arrived — most recent is yesterday 09:00.
    now = utc(2026, 6, 12, 8, 0)
    occ = reminders.current_occurrence(task("2026-06-01T09:00:00Z", DAY), now)
    assert occ == utc(2026, 6, 11, 9, 0)


def test_not_started_yet_returns_none():
    now = utc(2026, 6, 12, 10, 0)
    assert reminders.current_occurrence(task("2026-06-20T09:00:00Z", DAY), now) is None


def test_weekly_returns_this_weeks_slot():
    # Anchor Mon 2026-06-01 09:00; now Fri 2026-06-12 -> Mon 2026-06-08 slot.
    now = utc(2026, 6, 12, 10, 0)
    occ = reminders.current_occurrence(task("2026-06-01T09:00:00Z", WEEK), now)
    assert occ == utc(2026, 6, 8, 9, 0)


def test_monthly_uses_calendar_month():
    # Anchor day-of-month 15; now is the 12th, so the most recent slot is the
    # 15th of the PREVIOUS month, not 30 days back.
    now = utc(2026, 6, 12, 10, 0)
    occ = reminders.current_occurrence(task("2026-01-15T09:00:00Z", 30 * DAY, 1), now)
    assert occ == utc(2026, 5, 15, 9, 0)


def test_monthly_after_day_of_month():
    now = utc(2026, 6, 20, 10, 0)
    occ = reminders.current_occurrence(task("2026-01-15T09:00:00Z", 30 * DAY, 1), now)
    assert occ == utc(2026, 6, 15, 9, 0)


def test_monthly_clamps_to_end_of_short_month():
    # Anchor on the 31st -> February has no 31st, clamps to the 28th.
    now = utc(2026, 2, 28, 10, 0)
    occ = reminders.current_occurrence(task("2026-01-31T09:00:00Z", 30 * DAY, 1), now)
    assert occ == utc(2026, 2, 28, 9, 0)


def test_after_downtime_fires_only_the_latest_occurrence():
    # Bot was down for days; we fire the single most recent slot, not a backlog.
    now = utc(2026, 6, 12, 10, 0)
    occ = reminders.current_occurrence(task("2026-06-01T09:00:00Z", DAY), now)
    assert occ == utc(2026, 6, 12, 9, 0)  # one slot, today


def test_notification_key_changes_per_occurrence():
    t = task("2026-06-01T09:00:00Z", DAY)
    key_day1 = reminders._recurring_notification(t, utc(2026, 6, 11, 10, 0))
    key_day2 = reminders._recurring_notification(t, utc(2026, 6, 12, 10, 0))
    assert key_day1 and key_day2 and key_day1 != key_day2


def test_notification_none_before_start():
    t = task("2026-06-20T09:00:00Z", DAY)
    assert reminders._recurring_notification(t, utc(2026, 6, 12, 10, 0)) is None
