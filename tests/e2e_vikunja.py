"""End-to-end test of the Vikunja API client against a REAL instance.

Not run by pytest by default (needs a live server). Usage:

    docker compose up -d vikunja   # then create an API token in the UI
    E2E_VIKUNJA_URL=http://localhost:3456 E2E_VIKUNJA_TOKEN=tk_... \
        python tests/e2e_vikunja.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))

import vikunja  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name} {detail}")


async def main() -> None:
    url = os.environ.get("E2E_VIKUNJA_URL", "")
    token = os.environ.get("E2E_VIKUNJA_TOKEN", "")
    if not (url and token):
        sys.exit("Set E2E_VIKUNJA_URL and E2E_VIKUNJA_TOKEN")

    client = vikunja.VikunjaClient(url, token)
    try:
        projects = await client.get_projects()
        check("get_projects returns a project", len(projects) >= 1)
        project_id = next(p["id"] for p in projects if p["id"] > 0)

        due = datetime.now(timezone.utc) + timedelta(hours=2)
        task = await client.create_task(
            project_id, "e2e: buy milk", due_date=due, priority=3
        )
        check("create_task returns id", task.get("id", 0) > 0)
        check("create_task sets due_date", vikunja.is_set(task.get("due_date")))
        check(
            "create_task sets a reminder",
            bool(task.get("reminders")) and vikunja.is_set(task["reminders"][0]["reminder"]),
        )
        check("create_task sets priority", task.get("priority") == 3)
        task_id = task["id"]

        open_tasks = await client.list_tasks("done = false")
        check("list_tasks filter finds it", any(t["id"] == task_id for t in open_tasks))

        snoozed_until = datetime.now(timezone.utc) + timedelta(days=1)
        snoozed = await client.snooze(task_id, snoozed_until)
        check(
            "snooze moves due_date",
            vikunja.parse_date(snoozed["due_date"]) - snoozed_until < timedelta(seconds=2),
        )
        check(
            "snooze preserves title (full-object update)",
            snoozed["title"] == "e2e: buy milk",
        )
        check("snooze preserves priority", snoozed.get("priority") == 3)

        done = await client.mark_done(task_id)
        check("mark_done flips done", done.get("done") is True)
        check(
            "mark_done preserves due_date",
            vikunja.parse_date(done["due_date"]) - snoozed_until < timedelta(seconds=2),
        )

        remaining = await client.list_tasks("done = false")
        check("done task leaves open list", all(t["id"] != task_id for t in remaining))

        await client.delete_task(task_id)
        fetched = None
        try:
            fetched = await client.get_task(task_id)
        except vikunja.VikunjaError:
            pass
        check("delete_task removes it", fetched is None or fetched.get("id") != task_id)
    finally:
        await client.close()

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
