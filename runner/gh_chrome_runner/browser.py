from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

from gh_chrome_protocol import SessionParams

from gh_chrome_runner.cdp import Cdp
from gh_chrome_runner.config import settings
from gh_chrome_runner.display import Display

log = logging.getLogger(__name__)

READY_TIMEOUT = 60.0

FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-background-networking",
    "--disable-component-update",
    "--no-service-autorun",
    "--homepage=about:blank",
)


def _no_sandbox_needed() -> bool:
    userns = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if userns.exists() and userns.read_text().strip() == "0":
        return True
    return Path("/.dockerenv").exists() or "GITHUB_ACTIONS" in os.environ


class Browser:
    def __init__(self, display: Display, params: SessionParams) -> None:
        self._display = display
        self._params = params
        self._process: asyncio.subprocess.Process | None = None
        self.cdp: Cdp | None = None

    async def start(self) -> None:
        settings.profile_dir.mkdir(parents=True, exist_ok=True)
        settings.downloads_dir.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        command = [
            settings.chrome,
            f"--remote-debugging-port={settings.debug_port}",
            f"--user-data-dir={settings.profile_dir}",
            f"--window-size={self._params.width},{self._params.height}",
            "--window-position=0,0",
            *FLAGS,
        ]
        if settings.proxy:
            command.append(f"--proxy-server={settings.proxy}")
        if _no_sandbox_needed():
            command.append("--no-sandbox")
        command.append("about:blank")
        handle = (settings.logs_dir / "chrome.log").open("ab")
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=handle,
            stderr=asyncio.subprocess.STDOUT,
            env=self._display.env,
        )
        endpoint = await self._wait_endpoint()
        self.cdp = Cdp(endpoint)
        await self.cdp.connect()
        await self.cdp.send("Target.setDiscoverTargets", {"discover": True})
        await self.cdp.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        await self.cdp.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": str(settings.downloads_dir),
                "eventsEnabled": True,
            },
        )

    async def stop(self) -> None:
        if self.cdp is not None:
            with contextlib.suppress(Exception):
                await self.cdp.send("Browser.close")
            await self.cdp.close()
            self.cdp = None
        if self._process is None or self._process.returncode is not None:
            return
        self._process.terminate()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(20):
                await self._process.wait()
        if self._process.returncode is None:
            self._process.kill()
            await self._process.wait()

    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _wait_endpoint(self) -> str:
        deadline = asyncio.get_running_loop().time() + READY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                raise RuntimeError(f"chrome exited with {self._process.returncode}")
            try:
                version: dict[str, Any] = await Cdp.version(settings.debug_port)
            except Exception:
                await asyncio.sleep(0.3)
                continue
            endpoint = version.get("webSocketDebuggerUrl")
            if isinstance(endpoint, str):
                return endpoint
            await asyncio.sleep(0.3)
        raise RuntimeError("chrome did not expose a debugging endpoint")
