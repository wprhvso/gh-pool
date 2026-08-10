import asyncio
import contextlib
import os

from gh_chrome_runner.config import settings

READY_TIMEOUT = 20.0


class Display:
    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height
        self._processes: list[asyncio.subprocess.Process] = []

    @property
    def name(self) -> str:
        return settings.display_name

    @property
    def env(self) -> dict[str, str]:
        return os.environ | {"DISPLAY": self.name}

    async def start(self) -> None:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        await self._spawn(
            "xvfb",
            "Xvfb", self.name,
            "-screen", "0", f"{self._width}x{self._height}x24",
            "-nolisten", "tcp",
            "-dpi", "96",
        )  # fmt: skip
        await self._wait_ready()
        await self._spawn("openbox", "openbox", "--sm-disable")
        await asyncio.sleep(0.5)

    async def stop(self) -> None:
        for process in reversed(self._processes):
            if process.returncode is not None:
                continue
            process.terminate()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(5):
                    await process.wait()
            if process.returncode is None:
                process.kill()

    def alive(self) -> bool:
        return bool(self._processes) and all(
            p.returncode is None for p in self._processes
        )

    async def _spawn(self, log_name: str, *command: str) -> None:
        handle = (settings.logs_dir / f"{log_name}.log").open("ab")
        self._processes.append(
            await asyncio.create_subprocess_exec(
                *command, stdout=handle, stderr=asyncio.subprocess.STDOUT, env=self.env
            )
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
