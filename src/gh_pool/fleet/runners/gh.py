from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from gh_pool.fleet.runners.budget import REST
from gh_pool.fleet.runners.config import (
    PREFLIGHT_TTL,
    RELEASE_TTL,
    REST_VERSION,
    RUNNER_NAME_PREFIX,
    RUNNER_RELEASES,
    api_base,
)
from gh_pool.fleet.runners.errors import HttpError, RateLimited, RunnerError
from gh_pool.fleet.runners.http import fetch, request

if TYPE_CHECKING:
    from gh_pool.fleet.runners.config import Target

log = logging.getLogger(__name__)

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": REST_VERSION,
}
_FORBIDDEN = 403
_NOT_FOUND = 404

_lock = threading.Lock()
_checked: dict[str, float] = {}
_release: dict[str, float] = {}


def rest(
    token: str, method: str, path: str, *, body: object = None, attempts: int = 3
) -> Any:
    return request(
        method,
        api_base() + path,
        auth=f"Bearer {token}",
        body=body,
        attempts=attempts,
        extra=_HEADERS,
        budget=REST,
    )


def repo(target: Target) -> dict[str, Any]:
    try:
        answer = rest(target.token, "GET", f"/repos/{target.slug}")
    except RateLimited:
        raise
    except HttpError as exc:
        if exc.status == _NOT_FOUND:
            raise RunnerError(
                f"{target.slug} не виден этому токену: нет репозитория или прав"
            ) from None
        raise
    if not isinstance(answer, dict):
        raise RunnerError(f"не разобрал ответ про {target.slug}")
    return answer


def registration_token(target: Target) -> str:
    answer = rest(
        target.token, "POST", f"/repos/{target.slug}/actions/runners/registration-token"
    )
    value = str((answer or {}).get("token", ""))
    if not value:
        raise RunnerError(f"пустой registration token для {target.slug}")
    return value


def runners(target: Target) -> list[dict[str, Any]]:
    answer = rest(
        target.token, "GET", f"/repos/{target.slug}/actions/runners?per_page=100"
    )
    found = answer.get("runners", []) if isinstance(answer, dict) else []
    return [
        item
        for item in found
        if isinstance(item, dict)
        and str(item.get("name", "")).startswith(RUNNER_NAME_PREFIX)
    ]


def delete_runner(target: Target, runner_id: int) -> bool:
    try:
        rest(
            target.token,
            "DELETE",
            f"/repos/{target.slug}/actions/runners/{runner_id}",
            attempts=2,
        )
    except RunnerError as exc:
        log.debug("раннер %s не удалён: %s", runner_id, exc)
        return False
    return True


def release_version() -> str:
    now = time.monotonic()
    with _lock:
        for tag, at in _release.items():
            if now - at < RELEASE_TTL:
                return tag
    landed = fetch("HEAD", RUNNER_RELEASES, attempts=2).url
    tag = landed.rsplit("/v", 1)[-1].strip("/") if "/tag/v" in landed else ""
    if not tag:
        raise RunnerError(f"не определил версию actions/runner: {landed}")
    with _lock:
        _release.clear()
        _release[tag] = time.monotonic()
    return tag


def preflight(target: Target) -> dict[str, Any]:
    with _lock:
        at = _checked.get(target.slug)
    info = repo(target)
    if at is not None and time.monotonic() - at < PREFLIGHT_TTL:
        return info

    try:
        rest(
            target.token,
            "GET",
            f"/repos/{target.slug}/actions/runners?per_page=1",
            attempts=2,
        )
    except RateLimited:
        raise
    except HttpError as exc:
        if exc.status == _FORBIDDEN:
            raise RunnerError(
                f"нет прав администратора на {target.slug} — они нужны, чтобы завести scale set"
            ) from None
        raise

    with _lock:
        _checked[target.slug] = time.monotonic()
    return info
