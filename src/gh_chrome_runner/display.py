from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from gh_chrome_runner.config import settings

log = logging.getLogger(__name__)

READY_TIMEOUT = 20.0


class Display:
    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height
        self._xvfb: asyncio.subprocess.Process | None = None
        self._wm: asyncio.subprocess.Process | None = None

    @property
    def name(self) -> str:
        return settings.display_name

    @property
    def env(self) -> dict[str, str]:
        return os.environ | {"DISPLAY": self.name}

    async def start(self) -> None:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        self._xvfb = await self._spawn(
            "Xvfb",
            self.name,
            "-screen",
            "0",
            f"{self._width}x{self._height}x24",
            "-nolisten",
            "tcp",
            "-dpi",
            "96",
            log_name="xvfb",
        )
        await self._wait_ready()
        self._wm = await self._spawn("openbox", "--sm-disable", log_name="openbox", display=True)
        await asyncio.sleep(0.5)

    async def stop(self) -> None:
        for process in (self._wm, self._xvfb):
            if process is None or process.returncode is not None:
                continue
            process.terminate()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(5):
                    await process.wait()
            if process.returncode is None:
                process.kill()

    def alive(self) -> bool:
        return all(
            process is not None and process.returncode is None for process in (self._xvfb, self._wm)
        )

    async def _spawn(
        self, *command: str, log_name: str, display: bool = False
    ) -> asyncio.subprocess.Process:
        path: Path = settings.logs_dir / f"{log_name}.log"
        handle = path.open("ab")
        return await asyncio.create_subprocess_exec(
            *command,
            stdout=handle,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env if display else None,
        )

    async def _wait_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + READY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            probe = await asyncio.create_subprocess_exec(
                "xdpyinfo",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
            )
            if await probe.wait() == 0:
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"Xvfb on {self.name} did not become ready")
