import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from pool.protocol import SessionParams
from pool.browser.cdp import Cdp
from pool.browser.config import settings
from pool.browser.display import Display

READY_TIMEOUT = 60.0
CLOSE_TIMEOUT = 5.0

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


def _sandbox_unavailable() -> bool:
    userns = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if userns.exists() and userns.read_text().strip() == "0":
        return True
    return Path("/.dockerenv").exists() or "GITHUB_ACTIONS" in os.environ


class Browser:
    def __init__(self, display: Display, params: SessionParams) -> None:
        self._display = display
        self._params = params
        self._process: asyncio.subprocess.Process | None = None
        self._cdp: Cdp | None = None

    @property
    def cdp(self) -> Cdp:
        if self._cdp is None:
            raise RuntimeError("chrome is not connected")
        return self._cdp

    async def start(self) -> None:
        for directory in (
            settings.profile_dir,
            settings.downloads_dir,
            settings.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
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
        if _sandbox_unavailable():
            command.append("--no-sandbox")
        command.append("about:blank")
        handle = (settings.logs_dir / "chrome.log").open("ab")
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=handle,
            stderr=asyncio.subprocess.STDOUT,
            env=self._display.env,
        )
        self._cdp = Cdp(await self._wait_endpoint())
        await self._cdp.connect()
        await self._cdp.send("Target.setDiscoverTargets", {"discover": True})
        await self._cdp.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        await self._cdp.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": str(settings.downloads_dir),
                "eventsEnabled": True,
            },
        )

    async def stop(self) -> None:
        if self._cdp is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await self._cdp.send("Browser.close")
            await self._cdp.close()
            self._cdp = None
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
            with contextlib.suppress(Exception):
                version: dict[str, Any] = await Cdp.version(settings.debug_port)
                endpoint = version.get("webSocketDebuggerUrl")
                if isinstance(endpoint, str):
                    return endpoint
            await asyncio.sleep(0.3)
        raise RuntimeError("chrome did not expose a debugging endpoint")
