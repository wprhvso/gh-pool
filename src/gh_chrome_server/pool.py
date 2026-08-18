from importlib.metadata import PackageNotFoundError, version
from uuid import UUID

import httpx

from gh_chrome_server.config import settings

CODE = """
import os
import sys
from pathlib import Path


def run(session_id, url, token, workdir):
    root = Path(workdir) / session_id
    root.mkdir(parents=True, exist_ok=True)
    os.environ["GH_CHROME_URL"] = url
    os.environ["GH_CHROME_TOKEN"] = token
    os.environ["GH_CHROME_WORKDIR"] = str(root)

    from gh_chrome_runner.__main__ import main

    sys.argv = ["gh-chrome-runner", "--session", session_id]
    try:
        main()
    except SystemExit as exit_code:
        if exit_code.code:
            raise
    return session_id
"""


class DispatchError(Exception):
    pass


def runner_spec() -> str:
    if settings.runner_spec:
        return settings.runner_spec
    try:
        return f"gh-chrome[runner]=={version('gh-chrome')}"
    except PackageNotFoundError as missing:
        raise DispatchError("cannot tell which runner to install") from missing


async def dispatch(session_id: UUID, runner_token: str) -> None:
    if not settings.pool_server or not settings.pool_token:
        raise DispatchError("pool is not configured")
    body = {
        "type": "python",
        "payload": {
            "code": CODE,
            "entry": "run",
            "deps": [runner_spec()],
            "timeout": settings.runner_timeout,
            "kwargs": {
                "session_id": str(session_id),
                "url": settings.public_url,
                "token": runner_token,
                "workdir": str(settings.runner_workdir),
            },
        },
    }
    headers = {"Authorization": f"Bearer {settings.pool_token}"}
    url = f"{settings.pool_server.rstrip('/')}/v1/tasks"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=body, headers=headers)
    if response.status_code >= int(httpx.codes.BAD_REQUEST):
        raise DispatchError(f"{response.status_code}: {response.text[:200]}")
    if not (response.json() or {}).get("task_id"):
        raise DispatchError("pool returned no task_id")
