"""Vikunja Telegram Assistant — chat your todos onto a Vikunja board and
get reminders back in Telegram.
"""

import logging
from datetime import datetime, time, timedelta

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import llm
import parsing
import reminders
import vikunja

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

PROJECT_CACHE_SECONDS = 300


# ── Helpers ──────────────────────────────────────────────────────────────────


def authorized(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id in config.ALLOWED_USER_IDS


async def get_projects(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Projects list, cached briefly so #project resolution stays snappy."""
    cache = context.application.bot_data
    now = datetime.now(config.TIMEZONE)
    if "projects" not in cache or (now - cache["projects_at"]).total_seconds() > PROJECT_CACHE_SECONDS:
        cache["projects"] = await cache["vikunja"].get_projects()
        cache["projects_at"] = now
    return cache["projects"]


def resolve_project(hint: str | None, projects: list[dict]) -> int:
    if hint:
        hint = hint.lower()
        for project in projects:
            if project["title"].lower().startswith(hint):
                return project["id"]
    return config.DEFAULT_PROJECT_ID


def format_task_line(task: dict) -> str:
    line = f"<code>{task['id']}</code> {task['title']}"
    if vikunja.is_set(task.get("due_date")):
        due = vikunja.parse_date(task["due_date"]).astimezone(config.TIMEZONE)
        line += f" — 📅 {due:%a %d %b %H:%M}"
    if vikunja.is_recurring(task):
        line += " 🔁"
    if task.get("priority"):
        line += f" {'❗' * min(task['priority'], 3)}"
    return line


def next_occurrence(hour: int) -> datetime:
    """The next time today/tomorrow that the clock hits `hour` (local)."""
    now = datetime.now(config.TIMEZONE)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def done_message(task: dict) -> str:
    """Confirmation after completing a task. Vikunja rolls a recurring task
    forward (done flips back to false, due_date advances) — say when it's next."""
    if vikunja.is_recurring(task) and vikunja.is_set(task.get("due_date")):
        nxt = vikunja.parse_date(task["due_date"]).astimezone(config.TIMEZONE)
        return f"✅ Done: {task['title']}\n🔁 back on {nxt:%a %d %b %H:%M}"
    return f"✅ Done: {task['title']}"


# ── Capture (plain text and /add) ────────────────────────────────────────────


async def capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text(
            f"Not authorized. Your Telegram user ID is {update.effective_user.id} — "
            "add it to TELEGRAM_ALLOWED_USER_IDS to use this bot."
        )
        return

    text = update.message.text
    if text.startswith("/add"):
        text = text[4:].strip()
    if not text:
        await update.message.reply_text("Usage: /add buy milk tomorrow 6pm #personal !3")
        return

    logger.info("Capture from %s: %r", update.effective_user.id, text[:100])
    projects = await get_projects(context)
    parsed_tasks = None
    if config.LLM_ENABLED:
        parsed_tasks = await llm.parse_with_llm(text, [p["title"] for p in projects])
    if parsed_tasks is None:  # LLM off or failed — fall back to the date parser
        parsed_tasks = [
            {
                "title": r.title,
                "due_date": r.due_date,
                "priority": r.priority,
                "project": r.project_hint,
                "repeat_after": r.repeat_after,
                "repeat_mode": r.repeat_mode,
            }
            for r in parsing.parse_messages(text, config.TIMEZONE_NAME, config.DATEPARSER_LANGUAGES)
        ]

    client = context.application.bot_data["vikunja"]
    created: list[tuple[dict, int]] = []
    for parsed in parsed_tasks:
        project_id = resolve_project(parsed.get("project"), projects)
        due_date = parsed.get("due_date")
        # A recurring task needs a due date to roll forward from; if the user
        # gave a cadence but no time ("100 videos per day"), anchor the first
        # occurrence to the default reminder hour.
        if parsed.get("repeat_after") and due_date is None:
            due_date = next_occurrence(config.DEFAULT_REMINDER_HOUR)
        try:
            task = await client.create_task(
                project_id,
                parsed["title"],
                due_date=due_date,
                priority=parsed.get("priority") or None,
                repeat_after=parsed.get("repeat_after"),
                repeat_mode=parsed.get("repeat_mode") or 0,
            )
        except vikunja.VikunjaError as exc:
            await update.message.reply_text(f"😞 {exc}")
            return
        created.append((task, project_id))

    if not created:
        await update.message.reply_text("🤔 I couldn't find a task in that message.")
        return

    logger.info("Created %d task(s) for %s", len(created), update.effective_user.id)
    if len(created) == 1:
        await _confirm_single(update, created[0], projects)
    else:
        await _confirm_many(update, created)


async def _confirm_single(update: Update, created: tuple[dict, int], projects: list[dict]) -> None:
    task, project_id = created
    project_title = next((p["title"] for p in projects if p["id"] == project_id), "?")
    confirmation = f"✅ Added to <b>{project_title}</b>: {task['title']}"
    if vikunja.is_set(task.get("due_date")):
        due_local = vikunja.parse_date(task["due_date"]).astimezone(config.TIMEZONE)
        confirmation += f"\n📅 {due_local:%a %d %b %H:%M} (you'll get a reminder)"
    if vikunja.is_recurring(task):
        confirmation += (
            f"\n🔁 repeats {vikunja.describe_recurrence(task)} — "
            "I'll remind you every time, done or not"
        )
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Done", callback_data=f"done:{task['id']}"),
                InlineKeyboardButton("🗑 Undo", callback_data=f"undo:{task['id']}"),
                InlineKeyboardButton(
                    "🔗 Open board", url=f"{config.VIKUNJA_PUBLIC_URL}/projects/{project_id}"
                ),
            ]
        ]
    )
    await update.message.reply_text(confirmation, parse_mode="HTML", reply_markup=buttons)


async def _confirm_many(update: Update, created: list[tuple[dict, int]]) -> None:
    lines = [format_task_line(task) for task, _ in created]
    text = f"✅ Added {len(created)} tasks:\n" + "\n".join(lines) + "\n\n/done &lt;id&gt; to complete one"
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔗 Open board", url=config.VIKUNJA_PUBLIC_URL)]]
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=buttons)


# ── Commands ─────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not authorized(update):
        await update.message.reply_text(
            f"👋 Your Telegram user ID is {user_id}.\n"
            "Add it to TELEGRAM_ALLOWED_USER_IDS in .env to start using the bot."
        )
        return
    await update.message.reply_text(
        "👋 Just type a todo and I'll put it on your board:\n\n"
        "  <i>buy milk tomorrow 6pm #personal !3</i>\n\n"
        "Send a bulleted list and I'll add each line as its own task.\n"
        "I'll remind you here when things are due.\n\n"
        f"🔗 Your board: {config.VIKUNJA_PUBLIC_URL}\n"
        "/help for everything else.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(
        "Send any text to add a task. Optional bits, any order:\n"
        "  • a date: <i>tomorrow 6pm, friday, in 2 hours</i>\n"
        "  • recurrence: <i>every day, per day, weekly, every 3 days, every monday</i>\n"
        "  • <code>#project</code> — first matching project name\n"
        "  • <code>!1</code>–<code>!5</code> — priority\n"
        "A bulleted/numbered list adds one task per line (a header like "
        "<i>“…until 6pm:”</i> applies to all).\n"
        "A recurring task 🔁 reminds you every period on schedule — done or not.\n\n"
        "Commands:\n"
        "  /list — open tasks\n"
        "  /today — due or overdue today\n"
        "  /done &lt;id&gt; — complete a task\n"
        "  /projects — your projects\n"
        "  /board — open the web UI (also /web, /open, /fe)",
        parse_mode="HTML",
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    client = context.application.bot_data["vikunja"]
    try:
        tasks = await client.list_tasks("done = false", per_page=25)
    except vikunja.VikunjaError as exc:
        await update.message.reply_text(f"😞 {exc}")
        return
    if not tasks:
        await update.message.reply_text("🎉 Nothing open!")
        return
    lines = [format_task_line(t) for t in tasks]
    await update.message.reply_text(
        "<b>Open tasks</b>\n" + "\n".join(lines) + "\n\n/done &lt;id&gt; to complete one",
        parse_mode="HTML",
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    client = context.application.bot_data["vikunja"]
    try:
        tasks = await client.list_tasks("done = false && due_date < now/d+1d", per_page=25)
    except vikunja.VikunjaError as exc:
        await update.message.reply_text(f"😞 {exc}")
        return
    tasks = [t for t in tasks if vikunja.is_set(t.get("due_date"))]
    if not tasks:
        await update.message.reply_text("🎉 Nothing due today!")
        return
    lines = [format_task_line(t) for t in tasks]
    await update.message.reply_text("<b>Due today / overdue</b>\n" + "\n".join(lines), parse_mode="HTML")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /done <task id> (ids are shown by /list)")
        return
    client = context.application.bot_data["vikunja"]
    try:
        task = await client.mark_done(int(context.args[0]))
    except vikunja.VikunjaError as exc:
        await update.message.reply_text(f"😞 {exc}")
        return
    await update.message.reply_text(done_message(task))


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    try:
        projects = await get_projects(context)
    except vikunja.VikunjaError as exc:
        await update.message.reply_text(f"😞 {exc}")
        return
    lines = [
        f"<code>{p['id']}</code> {p['title']}"
        + (" ⭐ (default)" if p["id"] == config.DEFAULT_PROJECT_ID else "")
        for p in projects
        if p["id"] > 0  # skip pseudo-projects like Favorites (-1)
    ]
    await update.message.reply_text(
        "<b>Projects</b>\n" + "\n".join(lines) + "\n\nUse #name when adding a task.",
        parse_mode="HTML",
    )


async def cmd_board(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔗 Open board", url=config.VIKUNJA_PUBLIC_URL)]]
    )
    await update.message.reply_text(
        f"🔗 Your board:\n{config.VIKUNJA_PUBLIC_URL}",
        reply_markup=buttons,
        disable_web_page_preview=True,
    )


# ── Inline buttons ───────────────────────────────────────────────────────────


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not authorized(update):
        await query.answer("Not authorized.")
        return
    client = context.application.bot_data["vikunja"]
    action, _, rest = query.data.partition(":")
    try:
        if action == "done":
            task = await client.mark_done(int(rest))
            await query.answer("Done ✅")
            await query.edit_message_text(done_message(task))
        elif action == "undo":
            await client.delete_task(int(rest))
            await query.answer("Removed")
            await query.edit_message_text("🗑 Removed.")
        elif action == "snooze":
            task_id, _, amount = rest.partition(":")
            if amount == "tomorrow":
                local_now = datetime.now(config.TIMEZONE)
                until = datetime.combine(
                    local_now.date() + timedelta(days=1), time(9, 0), tzinfo=config.TIMEZONE
                )
            else:
                until = datetime.now(config.TIMEZONE) + timedelta(seconds=int(amount))
            task = await client.snooze(int(task_id), until)
            await query.answer("Snoozed ⏰")
            await query.edit_message_text(
                f"⏰ Snoozed until {until:%a %d %b %H:%M}: {task['title']}"
            )
        else:
            await query.answer()
    except vikunja.VikunjaError as exc:
        await query.answer(str(exc)[:190], show_alert=True)


# ── Wiring ───────────────────────────────────────────────────────────────────


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Never fail silently: log the exception and tell the user something broke."""
    logger.error("Unhandled handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "😞 Something went wrong handling that — it may not have been saved. Try again."
            )
        except Exception:  # the reply itself can fail (e.g. network) — swallow it
            pass


async def post_init(application: Application) -> None:
    application.bot_data["vikunja"] = vikunja.VikunjaClient(
        config.VIKUNJA_URL, config.VIKUNJA_API_TOKEN
    )
    # Register the command menu so they show under the "/" button in Telegram.
    await application.bot.set_my_commands(
        [
            BotCommand("list", "Open tasks"),
            BotCommand("today", "Due or overdue today"),
            BotCommand("board", "Open the board (web UI)"),
            BotCommand("projects", "Your projects"),
            BotCommand("done", "Complete a task by id"),
            BotCommand("help", "How to use the bot"),
        ]
    )
    logger.info(
        "Bot up. Vikunja: %s | LLM parsing: %s | poll every %ss",
        config.VIKUNJA_URL,
        "on" if config.LLM_ENABLED else "off (dateparser)",
        config.REMINDER_POLL_SECONDS,
    )


async def post_shutdown(application: Application) -> None:
    await application.bot_data["vikunja"].close()


def main() -> None:
    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("add", capture))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("done", cmd_done))
    application.add_handler(CommandHandler("projects", cmd_projects))
    application.add_handler(CommandHandler(["board", "web", "open", "link", "fe"], cmd_board))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))
    application.add_error_handler(on_error)

    application.job_queue.run_repeating(
        reminders.poll_reminders, interval=config.REMINDER_POLL_SECONDS, first=10
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
