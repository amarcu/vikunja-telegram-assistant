"""Vikunja Telegram Assistant — chat your todos onto a Vikunja board and
get reminders back in Telegram.
"""

import logging
from datetime import datetime, time, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    if task.get("priority"):
        line += f" {'❗' * min(task['priority'], 3)}"
    return line


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

    projects = await get_projects(context)
    parsed = None
    if config.LLM_ENABLED:
        parsed = await llm.parse_with_llm(text, [p["title"] for p in projects])
    if parsed is None:
        result = parsing.parse_message(text, config.TIMEZONE_NAME)
        parsed = {
            "title": result.title,
            "due_date": result.due_date,
            "priority": result.priority,
            "project": result.project_hint,
        }

    project_id = resolve_project(parsed.get("project"), projects)
    try:
        task = await context.application.bot_data["vikunja"].create_task(
            project_id,
            parsed["title"],
            due_date=parsed.get("due_date"),
            priority=parsed.get("priority") or None,
        )
    except vikunja.VikunjaError as exc:
        await update.message.reply_text(f"😞 {exc}")
        return

    project_title = next((p["title"] for p in projects if p["id"] == project_id), "?")
    confirmation = f"✅ Added to <b>{project_title}</b>: {task['title']}"
    if parsed.get("due_date"):
        due_local = parsed["due_date"].astimezone(config.TIMEZONE)
        confirmation += f"\n📅 {due_local:%a %d %b %H:%M} (you'll get a reminder)"
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
        "I'll remind you here when things are due. /help for everything else.",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(
        "Send any text to add a task. Optional bits, any order:\n"
        "  • a date: <i>tomorrow 6pm, friday, in 2 hours</i>\n"
        "  • <code>#project</code> — first matching project name\n"
        "  • <code>!1</code>–<code>!5</code> — priority\n\n"
        "Commands:\n"
        "  /list — open tasks\n"
        "  /today — due or overdue today\n"
        "  /done &lt;id&gt; — complete a task\n"
        "  /projects — your projects\n"
        "  /board — link to the web UI",
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
    await update.message.reply_text(f"✅ Done: {task['title']}")


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
    await update.message.reply_text(f"🔗 {config.VIKUNJA_PUBLIC_URL}")


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
            await query.edit_message_text(f"✅ Done: {task['title']}")
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


async def post_init(application: Application) -> None:
    application.bot_data["vikunja"] = vikunja.VikunjaClient(
        config.VIKUNJA_URL, config.VIKUNJA_API_TOKEN
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
    application.add_handler(CommandHandler("board", cmd_board))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture))

    application.job_queue.run_repeating(
        reminders.poll_reminders, interval=config.REMINDER_POLL_SECONDS, first=10
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
