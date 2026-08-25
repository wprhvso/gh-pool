from secrets import compare_digest

from fastapi import HTTPException

from gh_pool.core.config import settings


def _matches(given: str | None, token: str) -> bool:
    return given is not None and compare_digest(given, f"Bearer {token}")


def auth_worker(h: str | None) -> None:
    if not _matches(h, settings.worker_token):
        raise HTTPException(401, "bad worker token")


def auth_client(h: str | None) -> None:
    if not _matches(h, settings.client_token):
        raise HTTPException(401, "bad client token")


def auth_any(h: str | None) -> None:
    if not (_matches(h, settings.worker_token) or _matches(h, settings.client_token)):
        raise HTTPException(401, "bad token")
