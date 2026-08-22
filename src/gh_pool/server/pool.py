from importlib.metadata import PackageNotFoundError, version
from uuid import UUID

import httpx

from gh_pool.server.config import settings

CODE = """
import os
import subprocess
from pathlib import Path


def run(session_id, url, token, workdir, spec, python):
    root = Path(workdir) / session_id
    root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GH_POOL_SERVER"] = url
    env["GH_POOL_TOKEN"] = token
    env["GH_CHROME_WORKDIR"] = str(root)
    subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--python",
            python,
            "--with",
            spec,
            "gh-chrome-runner",
            "--session",
            session_id,
        ],
        env=env,
        check=True,
    )
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
            "timeout": settings.runner_timeout,
            "kwargs": {
                "session_id": str(session_id),
                "url": settings.public_url,
                "token": runner_token,
                "workdir": str(settings.runner_workdir),
                "spec": runner_spec(),
                "python": settings.runner_python,
            },
        },
    }
    headers = {"Authorization": f"Bearer {settings.pool_token}"}
    url = f"{settings.pool_server.rstrip('/')}/v1/tasks"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as unreachable:
        raise DispatchError(f"the pool is unreachable: {unreachable}") from unreachable
    if response.status_code >= int(httpx.codes.BAD_REQUEST):
        raise DispatchError(f"{response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError as unreadable:
        raise DispatchError("the pool answered with what is not json") from unreadable
    if not isinstance(payload, dict) or not payload.get("task_id"):
        raise DispatchError("pool returned no task_id")
