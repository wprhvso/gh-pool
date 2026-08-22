import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import Callable

from gh_chrome_runner.config import settings

log = logging.getLogger(__name__)

READY_TIMEOUT = 20.0


def _kasmvnc_command(name: str, width: int, height: int) -> list[str]:
    return [
        settings.kasmvnc_binary, name,
        "-geometry", f"{width}x{height}",
        "-depth", "24",
        "-dpi", "96",
        "-desktop", "gh-chrome",
        "-PublicIP", "127.0.0.1",
        "-interface", "127.0.0.1",
        "-websocketPort", str(settings.vnc_port),
        "-httpd", str(settings.vnc_www),
        "-http-header", "Cross-Origin-Embedder-Policy=require-corp",
        "-http-header", "Cross-Origin-Opener-Policy=same-origin",
        "-sslOnly", "0",
        "-SecurityTypes", "None",
        "-disableBasicAuth",
        "-BlacklistThreshold", "0",
        "-AlwaysShared",
        "-AcceptSetDesktopSize", "0",
        "-FrameRate", str(settings.vnc_frame_rate),
        "-Log", "*:stdout:30",
    ]  # fmt: skip


def _xvfb_command(name: str, width: int, height: int) -> list[str]:
    return [
        "Xvfb", name,
        "-screen", "0", f"{width}x{height}x24",
        "-nolisten", "tcp",
        "-dpi", "96",
    ]  # fmt: skip


class Display:
    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height
        self._processes: list[asyncio.subprocess.Process] = []
        self.vnc_port: int | None = None

    @property
    def name(self) -> str:
        return settings.display_name

    @property
    def env(self) -> dict[str, str]:
        return os.environ | {"DISPLAY": self.name}

    async def start(self) -> None:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        if self._kasmvnc_ready() and await self._try_server("xvnc", _kasmvnc_command):
            self.vnc_port = settings.vnc_port
            log.info("KasmVNC serves %s on port %d", self.name, self.vnc_port)
        elif await self._try_server("xvfb", _xvfb_command):
            log.info("Xvfb serves %s, the live desktop is off", self.name)
        else:
            raise RuntimeError(f"no X server came up on {self.name}")
        await self._spawn("openbox", "openbox", "--sm-disable")
        await asyncio.sleep(0.5)

    async def stop(self) -> None:
        for process in reversed(self._processes):
            await _terminate(process)

    def alive(self) -> bool:
        return bool(self._processes) and self._processes[0].returncode is None

    def _kasmvnc_ready(self) -> bool:
        if not settings.vnc:
            return False
        if shutil.which(settings.kasmvnc_binary) is None:
            log.warning("%s is not installed", settings.kasmvnc_binary)
            return False
        if not settings.vnc_www.is_dir():
            log.warning("the KasmVNC client is missing from %s", settings.vnc_www)
            return False
        return True

    async def _try_server(
        self, log_name: str, build: Callable[[str, int, int], list[str]]
    ) -> bool:
        await self._spawn(log_name, *build(self.name, self._width, self._height))
        if await self._wait_ready():
            return True
        log.warning(
            "%s did not answer on %s, see %s.log", log_name, self.name, log_name
        )
        await _terminate(self._processes.pop())
        return False

    async def _spawn(self, log_name: str, *command: str) -> None:
        handle = (settings.logs_dir / f"{log_name}.log").open("ab")
        self._processes.append(
            await asyncio.create_subprocess_exec(
                *command, stdout=handle, stderr=asyncio.subprocess.STDOUT, env=self.env
            )
        )

    async def _wait_ready(self) -> bool:
        server = self._processes[-1]
        deadline = asyncio.get_running_loop().time() + READY_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if server.returncode is not None:
                return False
            probe = await asyncio.create_subprocess_exec(
                "xdpyinfo",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
            )
            if await probe.wait() == 0:
                return True
            await asyncio.sleep(0.2)
        return False


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(5):
            await process.wait()
    if process.returncode is None:
        process.kill()
        await process.wait()
