from __future__ import annotations

import logging
import urllib.parse
from typing import TYPE_CHECKING, Any

from yaol import inject_headers

from pool_runners.config import ADOPT_LIMIT, ALIVE, POOL_TIMEOUT
from pool_runners.errors import RunnerError
from pool_runners.http import Reply, fetch

if TYPE_CHECKING:
    from pool_runners.config import Server

log = logging.getLogger("runners")

TYPE = "python"
_TAIL = 4000
_FAR = 1 << 40


class Pool:
    def __init__(self, server: Server) -> None:
        self.url: str = server.url.rstrip("/")
        self._auth: str = f"Bearer {server.token}"

    def call(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        params: dict[str, Any] | None = None,
        attempts: int = 3,
    ) -> Any:
        return self.raw(
            method, path, body=body, params=params, attempts=attempts
        ).json()

    def raw(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        params: dict[str, Any] | None = None,
        attempts: int = 3,
    ) -> Reply:
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return fetch(
            method,
            url,
            auth=self._auth,
            body=body,
            timeout=POOL_TIMEOUT,
            attempts=attempts,
        )

    def submit(
        self,
        code: str,
        entry: str,
        kwargs: dict[str, Any],
        timeout: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {"code": code, "entry": entry, "kwargs": kwargs}
        payload["trace"] = dict(inject_headers())
        if timeout:
            payload["timeout"] = timeout
        answer = self.call("POST", "/v1/tasks", body={"type": TYPE, "payload": payload})
        task_id = (answer or {}).get("task_id")
        if not task_id:
            raise RunnerError("пул не вернул task_id")
        return str(task_id)

    def state(self, task_id: str) -> dict[str, Any]:
        answer = self.call("GET", f"/v1/tasks/{task_id}")
        if not isinstance(answer, dict):
            raise RunnerError(f"пул не отдал задачу {task_id}")
        return answer

    def cancel(self, task_id: str) -> dict[str, Any]:
        answer = self.call("POST", f"/v1/tasks/{task_id}/cancel", attempts=2)
        return answer if isinstance(answer, dict) else {}

    def tail(self, task_id: str, limit: int = _TAIL) -> str:
        head = self.raw(
            "GET", f"/v1/tasks/{task_id}/events", params={"offset": _FAR}, attempts=2
        )
        size = int(head.headers.get("X-Event-Size") or 0)
        reply = self.raw(
            "GET",
            f"/v1/tasks/{task_id}/events",
            params={"offset": max(size - limit, 0)},
            attempts=2,
        )
        return reply.data.decode(errors="replace")

    def mine(self, slug: str, label: str) -> list[tuple[str, str, str]]:
        found: list[tuple[str, str, str]] = []
        for status in ALIVE:
            answer = self.call(
                "GET", "/v1/tasks", params={"status": status, "limit": ADOPT_LIMIT}
            )
            rows = answer if isinstance(answer, list) else []
            if len(rows) >= ADOPT_LIMIT:
                log.warning(
                    "пул отдал %s задач в статусе %s — свои старые раннеры могли не влезть",
                    len(rows),
                    status,
                )
            for row in rows:
                payload = row.get("payload") if isinstance(row, dict) else None
                kwargs = payload.get("kwargs") if isinstance(payload, dict) else None
                if not isinstance(kwargs, dict):
                    continue
                if kwargs.get("slug") != slug or kwargs.get("label", label) != label:
                    continue
                task_id = str(row.get("id") or "")
                if not task_id:
                    continue
                found.append((task_id, str(kwargs.get("name") or "?"), status))
        return found

    def workers(self) -> list[dict[str, Any]]:
        answer = self.call("GET", "/v1/workers", attempts=2)
        return answer if isinstance(answer, list) else []

    def health(self) -> dict[str, Any]:
        answer = self.call("GET", "/healthz", attempts=2)
        return answer if isinstance(answer, dict) else {}
