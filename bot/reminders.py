"""Reminder poller: checks Vikunja for due reminders and overdue tasks,
sends each one to Telegram exactly once (state kept in a small JSON file).
"""

import calendar
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
import vikunja

logger = logging.getLogger(__name__)

# Don't resurrect reminders older than this on first run / after downtime.
MAX_REMINDER_AGE = timedelta(hours=24)


def _load_state() -> dict:
    try:
        with open(config.STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"notified": {}}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    tmp = config.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, config.STATE_FILE)


def _prune(state: dict, now: datetime) -> None:
    cutoff = (now - timedelta(days=30)).isoformat()
    state["notified"] = {k: v for k, v in state["notified"].items() if v > cutoff}


def task_buttons(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Done", callback_data=f"done:{task_id}"),
                InlineKeyboardButton("⏰ +1h", callback_data=f"snooze:{task_id}:3600"),
                InlineKeyboardButton("🌅 Tomorrow", callback_data=f"snooze:{task_id}:tomorrow"),
            ]
        ]
    )


def recurring_buttons(task_id: int) -> InlineKeyboardMarkup:
    # No snooze: a recurring reminder is schedule-driven, so moving its due date
    # would shift the whole cadence. "Done" just rolls it to the next slot.
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Done for now", callback_data=f"done:{task_id}")]]
    )


def _due_notifications(task: dict, now: datetime) -> list[tuple[str, str]]:
    """Yield (state_key, kind) pairs that are due now for this task."""
    pending = []
    for reminder in task.get("reminders") or []:
        stamp = reminder.get("reminder")
        if not vikunja.is_set(stamp):
            continue
        when = vikunja.parse_date(stamp)
        if now - MAX_REMINDER_AGE <= when <= now:
            pending.append((f"reminder:{task['id']}:{stamp}", "reminder"))

    due = task.get("due_date")
    if vikunja.is_set(due):
        when = vikunja.parse_date(due)
        if now - MAX_REMINDER_AGE <= when <= now:
            pending.append((f"due:{task['id']}:{due}", "overdue"))
    return pending


# ── Recurring tasks (schedule-driven) ─────────────────────────────────────────
# A recurring task fires every period at its scheduled time regardless of
# whether it's been completed. We compute occurrences from the task's due_date
# (the anchor) + repeat_after, and fire each one exactly once via dedup. We do
# NOT advance the due date ourselves — completing the task is optional, and
# Vikunja's own roll-forward (a multiple of the period) keeps the grid aligned.


def _add_months(dt: datetime, months: int) -> datetime:
    index = dt.month - 1 + months
    year = dt.year + index // 12
    month = index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def current_occurrence(task: dict, now: datetime) -> datetime | None:
    """The most recent scheduled time at or before `now`, or None if the task
    hasn't started yet (anchor in the future) or isn't a valid recurrence."""
    due = task.get("due_date")
    if not vikunja.is_set(due):
        return None
    anchor = vikunja.parse_date(due)
    if anchor > now:
        return None

    if (task.get("repeat_mode") or 0) == 1:  # calendar-monthly
        months = (now.year - anchor.year) * 12 + (now.month - anchor.month)
        occurrence = _add_months(anchor, months)
        if occurrence > now:  # day-of-month not reached yet this month
            occurrence = _add_months(anchor, months - 1)
        return occurrence

    period = task.get("repeat_after") or 0
    if period <= 0:
        return None
    periods = int((now - anchor).total_seconds() // period)
    return anchor + timedelta(seconds=periods * period)


def _recurring_notification(task: dict, now: datetime) -> str | None:
    """Dedup key for this recurring task's current occurrence, if one is due."""
    occurrence = current_occurrence(task, now)
    if occurrence is None or occurrence > now:
        return None
    return f"recur:{task['id']}:{vikunja.format_date(occurrence)}"


async def _broadcast(context: ContextTypes.DEFAULT_TYPE, text: str, buttons) -> None:
    """Send one reminder to every allowlisted user; never let one failure
    abort the whole poll."""
    for user_id in config.ALLOWED_USER_IDS:
        try:
            await context.bot.send_message(
                chat_id=user_id, text=text, parse_mode="HTML", reply_markup=buttons
            )
        except Exception as exc:  # never let one user break the loop
            logger.warning("Failed to notify user %s: %s", user_id, exc)


async def poll_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    client = context.application.bot_data["vikunja"]
    now = datetime.now(timezone.utc)

    try:
        tasks = await client.list_tasks("done = false")
    except vikunja.VikunjaError as exc:
        logger.warning("Reminder poll failed: %s", exc)
        return

    state = _load_state()
    changed = False

    for task in tasks:
        # Recurring tasks fire on their own schedule, independent of done state.
        if vikunja.is_recurring(task):
            key = _recurring_notification(task, now)
            if key and key not in state["notified"]:
                cadence = vikunja.describe_recurrence(task)
                text = f"🔁 Reminder: <b>{task['title']}</b>\n<i>{cadence}</i>"
                await _broadcast(context, text, recurring_buttons(task["id"]))
                state["notified"][key] = datetime.now(config.TIMEZONE).isoformat()
                changed = True
            continue

        for key, kind in _due_notifications(task, now):
            if key in state["notified"]:
                continue
            icon = "🔔" if kind == "reminder" else "⚠️"
            label = "Reminder" if kind == "reminder" else "Overdue"
            text = f"{icon} {label}: <b>{task['title']}</b>"
            if vikunja.is_set(task.get("due_date")):
                due_local = vikunja.parse_date(task["due_date"]).astimezone(config.TIMEZONE)
                text += f"\n📅 due {due_local:%a %d %b %H:%M}"
            await _broadcast(context, text, task_buttons(task["id"]))
            state["notified"][key] = datetime.now(config.TIMEZONE).isoformat()
            changed = True

    if changed:
        _prune(state, now)
        _save_state(state)
