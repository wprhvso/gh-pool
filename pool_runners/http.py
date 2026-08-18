from __future__ import annotations

import base64
import binascii
import http.client
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pool_runners.config import (
    BACKOFF_BASE,
    BACKOFF_CAP,
    MAX_ATTEMPTS,
    REQUEST_TIMEOUT,
    RETRY_STATUSES,
    USER_AGENT,
)
from pool_runners.errors import HttpError, RateLimited, RunnerError
from pool_runners.redact import redact

if TYPE_CHECKING:
    from email.message import Message

    from pool_runners.budget import Budget

log = logging.getLogger("runners")

_FALLBACK_TTL = 1800.0
_TOO_MANY = 429
_NO_CONTENT = 204

STOP = threading.Event()


def pause(seconds: float) -> bool:
    if seconds <= 0:
        return STOP.is_set()
    return STOP.wait(seconds)


def guard(budget: Budget, url: str) -> None:
    waiting = budget.shut()
    if waiting:
        raise RateLimited(_TOO_MANY, url, "лимит ещё не сбросился", waiting)


def backoff(
    attempt: int, retry_after: str | None = None, cap: float = BACKOFF_CAP
) -> float:
    if retry_after:
        try:
            return min(float(retry_after), cap)
        except ValueError:
            pass
    return min(BACKOFF_BASE**attempt, cap) * (0.5 + random.random())


@dataclass(frozen=True)
class Reply:
    status: int
    data: bytes
    headers: Message
    url: str = ""

    def json(self) -> Any:
        if self.status == _NO_CONTENT or not self.data:
            return None
        try:
            return json.loads(self.data)
        except ValueError:
            raise RunnerError("ответ не разобрался как JSON") from None


def fetch(
    method: str,
    url: str,
    *,
    auth: str | None = None,
    body: object = None,
    data: bytes | None = None,
    timeout: int = REQUEST_TIMEOUT,
    attempts: int = MAX_ATTEMPTS,
    extra: dict[str, str] | None = None,
    budget: Budget | None = None,
) -> Reply:
    payload = data if body is None else json.dumps(body).encode()
    headers = {"User-Agent": USER_AGENT}
    if payload is not None:
        headers["Content-Type"] = (
            "application/json" if data is None else "application/octet-stream"
        )
    if extra:
        headers.update(extra)
    if auth:
        headers["Authorization"] = auth

    if budget is not None:
        guard(budget, url)

    last: Exception | None = None
    hurried = False
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                if budget is not None:
                    budget.observe(resp.headers)
                return Reply(resp.status, resp.read(), resp.headers, resp.geturl())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if budget is not None:
                budget.observe(exc.headers)
            waiting = (
                budget.refuse(exc.code, exc.headers, detail)
                if budget is not None
                else 0.0
            )
            if waiting:
                raise RateLimited(exc.code, url, detail, waiting) from None
            if exc.code not in RETRY_STATUSES:
                raise HttpError(exc.code, url, detail) from None
            last = HttpError(exc.code, url, detail)
            delay = backoff(attempt, exc.headers.get("Retry-After"))
        except (OSError, http.client.HTTPException) as exc:
            last = exc
            delay = backoff(attempt)

        if attempt + 1 >= attempts:
            break
        log.debug("повтор %s %s через %.1f с (%s)", method, redact(url), delay, last)
        stopping = pause(delay)
        if stopping and hurried:
            break
        hurried = hurried or stopping

    raise RunnerError(f"{method} {redact(url)} не удался за {attempts} попыток: {last}")


def request(method: str, url: str, **kw: Any) -> Any:
    return fetch(method, url, **kw).json()


def jwt_expiry(token: str) -> float:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return float(claims["exp"])
    except (IndexError, KeyError, TypeError, ValueError, binascii.Error):
        log.debug("не разобрал exp из токена, считаю его коротким")
        return time.time() + _FALLBACK_TTL
