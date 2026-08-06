from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path

from gh_chrome_protocol import RunnerConfig

from gh_chrome_runner.config import settings
from gh_chrome_runner.display import Display
from gh_chrome_runner.http import ServerClient

log = logging.getLogger(__name__)

SEGMENT_PATTERN = re.compile(r"chunk-stream0-(\d+)\.m4s$")
INIT_NAME = "init-stream0.m4s"
SCAN_INTERVAL = 0.5
STABLE_CHECKS = 2
RETRIES = 3


class Capture:
    def __init__(
        self, display: Display, server: ServerClient, config: RunnerConfig
    ) -> None:
        self._display = display
        self._server = server
        self._params = config.params
        self._segment_seconds = config.segment_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._sent: set[str] = set()
        self._sizes: dict[str, tuple[int, int]] = {}
        self._init_sent = False

    async def start(self) -> None:
        settings.segments_dir.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        bitrate = self._params.bitrate
        command = [
            settings.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "x11grab",
            "-draw_mouse",
            "1",
            "-framerate",
            str(self._params.fps),
            "-video_size",
            f"{self._params.width}x{self._params.height}",
            "-i",
            self._display.name,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "high",
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
            "-bufsize",
            bitrate,
            "-g",
            str(self._params.fps * 2),
            "-keyint_min",
            str(self._params.fps * 2),
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "dash",
            "-ldash",
            "1",
            "-streaming",
            "1",
            "-use_template",
            "1",
            "-use_timeline",
            "0",
            "-seg_duration",
            str(self._segment_seconds),
            "-remove_at_exit",
            "0",
            str(settings.segments_dir / "out.mpd"),
        ]
        handle = (settings.logs_dir / "ffmpeg.log").open("ab")
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=handle,
            stderr=asyncio.subprocess.STDOUT,
            env=self._display.env,
        )
        self._task = asyncio.create_task(self._watch())
        log.info("capture started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(15):
                    await self._process.wait()
            if self._process.returncode is None:
                self._process.kill()
                await self._process.wait()
        with contextlib.suppress(Exception):
            await self._scan(final=True)
        log.info("capture stopped after %d segments", len(self._sent))

    async def _watch(self) -> None:
        while True:
            if self._process is not None and self._process.returncode is not None:
                log.error(
                    "ffmpeg exited with %s, see %s",
                    self._process.returncode,
                    settings.logs_dir / "ffmpeg.log",
                )
                return
            with contextlib.suppress(Exception):
                await self._scan(final=False)
            await asyncio.sleep(SCAN_INTERVAL)

    async def _scan(self, final: bool) -> None:
        if not self._init_sent:
            init = settings.segments_dir / INIT_NAME
            if (
                init.exists()
                and init.stat().st_size > 0
                and await self._send("init", init)
            ):
                self._init_sent = True
        for path in sorted(settings.segments_dir.glob("chunk-stream0-*.m4s")):
            if path.name in self._sent:
                continue
            match = SEGMENT_PATTERN.search(path.name)
            if match is None:
                continue
            if not final and not self._stable(path):
                continue
            if await self._send(f"segments/{int(match.group(1))}", path):
                self._sent.add(path.name)

    def _stable(self, path: Path) -> bool:
        size = path.stat().st_size
        if size == 0:
            return False
        previous, checks = self._sizes.get(path.name, (-1, 0))
        if size != previous:
            self._sizes[path.name] = (size, 0)
            return False
        checks += 1
        self._sizes[path.name] = (size, checks)
        return checks >= STABLE_CHECKS

    async def _send(self, route: str, path: Path) -> bool:
        for attempt in range(RETRIES):
            try:
                await self._server.put_file(route, path)
            except Exception as exc:
                log.warning(
                    "failed to upload %s (attempt %d): %s", path.name, attempt + 1, exc
                )
                await asyncio.sleep(1.0)
            else:
                return True
        return False
