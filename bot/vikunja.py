"""Minimal async client for the Vikunja REST API (v1, Vikunja >= 1.0).

Gotchas this client encodes (verified against Vikunja 2.3):
- API-token auth requires an explicit `Accept: application/json` header.
- POST /tasks/{id} REPLACES the task: omitted fields are reset to zero
  values. Always GET the task, mutate, and POST the full object back.
- An unset date is the Go zero time `0001-01-01T00:00:00Z`, not null.
"""

from datetime import datetime, timezone

import httpx

ZERO_DATE = "0001-01-01T00:00:00Z"


def is_set(date_str: str | None) -> bool:
    """True if a Vikunja date field holds a real date."""
    return bool(date_str) and not date_str.startswith("0001-01-01")


def parse_date(date_str: str) -> datetime:
    """Parse a Vikunja RFC3339 date into an aware datetime (UTC)."""
    return datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(timezone.utc)


def format_date(dt: datetime) -> str:
    """Format an aware datetime as RFC3339 for the Vikunja API."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VikunjaError(Exception):
    pass


class VikunjaClient:
    def __init__(self, base_url: str, token: str):
        self._client = httpx.AsyncClient(
            base_url=f"{base_url}/api/v1",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=15,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise VikunjaError(f"Cannot reach Vikunja: {exc}") from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise VikunjaError(f"Vikunja API error {response.status_code}: {message}")
        return response

    # ── Projects ────────────────────────────────────────────────────────────

    async def get_projects(self) -> list[dict]:
        response = await self._request("GET", "/projects", params={"per_page": 100})
        return response.json()

    # ── Tasks ───────────────────────────────────────────────────────────────

    async def create_task(
        self,
        project_id: int,
        title: str,
        due_date: datetime | None = None,
        priority: int | None = None,
        description: str = "",
    ) -> dict:
        body: dict = {"title": title}
        if description:
            body["description"] = description
        if due_date is not None:
            body["due_date"] = format_date(due_date)
            # Also set an explicit reminder so the task shows up in
            # Vikunja's own reminder system, not just our poller.
            body["reminders"] = [{"reminder": format_date(due_date)}]
        if priority is not None:
            body["priority"] = priority
        response = await self._request("PUT", f"/projects/{project_id}/tasks", json=body)
        return response.json()

    async def list_tasks(self, filter_query: str, per_page: int = 100) -> list[dict]:
        response = await self._request(
            "GET",
            "/tasks",
            params={
                "filter": filter_query,
                "sort_by": "due_date",
                "order_by": "asc",
                "per_page": per_page,
            },
        )
        return response.json()

    async def get_task(self, task_id: int) -> dict:
        response = await self._request("GET", f"/tasks/{task_id}")
        return response.json()

    async def update_task(self, task: dict) -> dict:
        """POST the FULL task object back (partial updates wipe fields)."""
        response = await self._request("POST", f"/tasks/{task['id']}", json=task)
        return response.json()

    async def delete_task(self, task_id: int) -> None:
        await self._request("DELETE", f"/tasks/{task_id}")

    # ── Convenience ─────────────────────────────────────────────────────────

    async def mark_done(self, task_id: int) -> dict:
        task = await self.get_task(task_id)
        task["done"] = True
        return await self.update_task(task)

    async def snooze(self, task_id: int, until: datetime) -> dict:
        task = await self.get_task(task_id)
        task["due_date"] = format_date(until)
        task["reminders"] = [{"reminder": format_date(until)}]
        return await self.update_task(task)
