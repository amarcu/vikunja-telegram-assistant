# Vikunja Telegram Assistant

Chat your todos into [Vikunja](https://vikunja.io) and get reminded in Telegram.

```
you:  buy milk tomorrow 6pm #personal
bot:  ✅ Added to Personal: buy milk
      📅 Mon 08 Jun 18:00 (you'll get a reminder)

      …next day, 18:00…

bot:  🔔 Reminder: buy milk
      [✅ Done] [⏰ +1h] [🌅 Tomorrow]
```

Vikunja is a great self-hosted task manager with kanban boards, but it has
[no Telegram integration](https://community.vikunja.io/t/telegram-discord-notifications-reminders/489) —
you can't capture tasks from chat, and reminders don't reach you unless the
app is open. This bot closes both gaps with one small container:

- **Capture** — send any text, it becomes a task on your board. Natural dates
  ("tomorrow 6pm", "friday", "in 2 hours"), `#project`, `!1`–`!5` priority.
- **Remind** — polls Vikunja and pings you in Telegram when reminders fire or
  tasks go overdue, with inline **Done / Snooze** buttons.
- **Manage** — `/list`, `/today`, `/done`, `/projects` from chat; the full
  kanban board stays one tap away in Vikunja's web UI or mobile apps.
- **Optional AI** — point it at any OpenAI-compatible endpoint (Ollama,
  llama.cpp, OpenAI, …) for smarter parsing of messy messages. Entirely
  optional: the built-in [dateparser](https://github.com/scrapinghub/dateparser)
  path needs no AI, no GPU, no API key.

Single-user/household by design: a small allowlist of Telegram user IDs, one
Vikunja API token. ~500 lines of Python, no database of its own.

## Quick start

Prerequisites: Docker with Compose, a Telegram account.

```bash
git clone https://github.com/amarcu/vikunja-telegram-assistant
cd vikunja-telegram-assistant
cp .env.example .env
```

1. **Create the bot** — message [@BotFather](https://t.me/BotFather), `/newbot`,
   copy the token into `TELEGRAM_BOT_TOKEN`.
2. **Generate a secret** — `openssl rand -hex 32` into `VIKUNJA_JWT_SECRET`.
3. **Start Vikunja** — `docker compose up -d vikunja`, open
   <http://localhost:3456>, register your account and create a project.
4. **Create an API token** — Vikunja → Settings → API Tokens, with permissions
   for **tasks** (read, create, update, delete) and **projects** (read).
   Put it in `VIKUNJA_API_TOKEN`.
5. **Find your Telegram ID** — start the bot container
   (`docker compose up -d --build bot`), send `/start` to your bot; it replies
   with your user ID. Put it in `TELEGRAM_ALLOWED_USER_IDS` and
   `docker compose up -d bot` again.

That's it. Send your bot a todo.

> **Already running Vikunja?** Just run the bot container: set `VIKUNJA_URL`
> to your instance and `docker compose up -d --build bot`.

## Usage

| You send | What happens |
|---|---|
| `buy milk tomorrow 6pm` | Task in default project, due tomorrow 18:00, reminder set |
| `ship report friday #work !4` | Task in first project matching "work…", priority 4 |
| `call mom` | Task with no date — sits on the board until done |
| `/list` | Open tasks with IDs |
| `/today` | Due-today and overdue tasks |
| `/done 42` | Complete task 42 |
| `/projects` | Project list (for `#name`) |
| `/board` | Link to your web UI |

Reminder messages carry **✅ Done · ⏰ +1h · 🌅 Tomorrow** buttons, so most
days you never open the app.

## Optional: LLM parsing

Set three variables in `.env` to route free-form messages through any
OpenAI-compatible endpoint:

```bash
LLM_BASE_URL=http://your-ollama-host:11434/v1
LLM_API_KEY=            # empty for Ollama
LLM_MODEL=qwen3:8b
```

The LLM extracts title, due date, priority, and best-matching project from
messages like *"oh and I need to renew the car insurance sometime before next
friday, it's important"*. On any failure the bot silently falls back to the
built-in parser — the LLM is never a point of failure.

## Remote access while travelling

Pair this with [Tailscale](https://tailscale.com): the bot works anywhere
(Telegram is the transport), and `VIKUNJA_PUBLIC_URL` set to your machine's
Tailscale hostname makes the board buttons work from your phone too. No ports
exposed to the internet.

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | — | Comma-separated allowlist; also receives reminders |
| `VIKUNJA_URL` | `http://vikunja:3456` | Where the bot reaches the Vikunja API |
| `VIKUNJA_API_TOKEN` | — | Vikunja → Settings → API Tokens |
| `VIKUNJA_PUBLIC_URL` | `http://localhost:3456/` | Web-UI URL used in links/buttons |
| `VIKUNJA_JWT_SECRET` | — | Session secret for Vikunja itself |
| `DEFAULT_PROJECT_ID` | `1` | Project for tasks without `#project` |
| `TIMEZONE` | `Europe/Bucharest` | IANA TZ for parsing & display |
| `REMINDER_POLL_SECONDS` | `60` | Reminder check interval |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | unset | Optional AI parsing |

## Development

```bash
cd bot && python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest ../tests/
```

Architecture: `main.py` (handlers) · `parsing.py` (dateparser-based capture) ·
`llm.py` (optional AI capture) · `reminders.py` (poller + dedup state) ·
`vikunja.py` (API client). The client encodes three Vikunja API gotchas worth
knowing about if you hack on it: token auth needs an explicit
`Accept: application/json`; task updates **replace** the whole object
(GET-mutate-POST, never partial); unset dates are `0001-01-01T00:00:00Z`.

## License

[MIT](LICENSE). Vikunja itself is AGPLv3 and not bundled here — the compose
file pulls the official `vikunja/vikunja` image.
