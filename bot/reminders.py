"""Reminder poller: checks Vikunja for due reminders and overdue tasks,
sends each one to Telegram exactly once (state kept in a small JSON file).
"""

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
        for key, kind in _due_notifications(task, now):
            if key in state["notified"]:
                continue
            local = datetime.now(config.TIMEZONE)
            icon = "🔔" if kind == "reminder" else "⚠️"
            label = "Reminder" if kind == "reminder" else "Overdue"
            text = f"{icon} {label}: <b>{task['title']}</b>"
            if vikunja.is_set(task.get("due_date")):
                due_local = vikunja.parse_date(task["due_date"]).astimezone(config.TIMEZONE)
                text += f"\n📅 due {due_local:%a %d %b %H:%M}"
            for user_id in config.ALLOWED_USER_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=task_buttons(task["id"]),
                    )
                except Exception as exc:  # never let one user break the loop
                    logger.warning("Failed to notify user %s: %s", user_id, exc)
            state["notified"][key] = local.isoformat()
            changed = True

    if changed:
        _prune(state, now)
        _save_state(state)
