import asyncio
import contextlib
import logging

from gh_pool.core.config import settings
from gh_pool.core.sessions import Sessions
from gh_pool.protocol import CloseReason, CommandError, ErrorCode

log = logging.getLogger(__name__)

TIMED_OUT = CommandError(code=ErrorCode.TIMEOUT, message="command timed out")


class Watchdog:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("watchdog tick failed")
            await asyncio.sleep(settings.watchdog_interval)

    async def _tick(self) -> None:
        for row in await self._sessions.expired_commands():
            await self._sessions.complete(row["session_id"], row["id"], None, TIMED_OUT)
            self._sessions.request_cancel(row["session_id"], row["id"])
        for session_id in await self._sessions.dead_candidates(
            settings.heartbeat_timeout, settings.ready_timeout
        ):
            log.warning("session %s is dead", session_id)
            await self._sessions.finish(session_id, CloseReason.DEAD)
