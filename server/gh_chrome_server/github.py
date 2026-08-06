from __future__ import annotations

from uuid import UUID

import httpx

from gh_chrome_server.config import settings

API = "https://api.github.com"


class DispatchError(Exception):
    pass


async def dispatch(session_id: UUID) -> None:
    if not settings.github_repo or not settings.github_pat:
        raise DispatchError("github is not configured")
    url = (
        f"{API}/repos/{settings.github_repo}/actions/workflows/"
        f"{settings.github_workflow}/dispatches"
    )
    payload = {"ref": settings.github_ref, "inputs": {"session_id": str(session_id)}}
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.github_pat}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code != int(httpx.codes.NO_CONTENT):
        raise DispatchError(f"{response.status_code}: {response.text[:200]}")
