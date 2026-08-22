import asyncio
import contextlib
import logging
from uuid import UUID

from gh_pool.server import storage
from gh_pool.server.config import settings
from gh_pool.server.sessions import Sessions

log = logging.getLogger(__name__)

DAY = 86400.0
MIB = 1 << 20


class Cleaner:
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
        await asyncio.sleep(settings.cleanup_delay)
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("cleanup tick failed")
            await asyncio.sleep(settings.cleanup_interval)

    async def _tick(self) -> None:
        removed = 0
        freed = 0
        for session_id in await self._sessions.closed_before(
            settings.cleanup_max_days * DAY
        ):
            freed += await self._forget(session_id)
            removed += 1
        limit = settings.cleanup_max_bytes
        used = await asyncio.to_thread(storage.sessions_size)
        if used > limit:
            for session_id in await self._sessions.closed_before(settings.runner_grace):
                if used <= limit:
                    break
                size = await self._forget(session_id)
                used -= size
                freed += size
                removed += 1
        if removed:
            log.info("removed %d sessions, freed %.1f MiB", removed, freed / MIB)
        if used > limit:
            log.warning(
                "nothing left to remove: sessions hold %.1f MiB of the %.1f MiB allowed",
                used / MIB,
                limit / MIB,
            )

    async def _forget(self, session_id: UUID) -> int:
        size = await asyncio.to_thread(storage.session_size, session_id)
        await asyncio.to_thread(storage.remove_session, session_id)
        await self._sessions.forget(session_id)
        log.info("session %s is gone, %.1f MiB back", session_id, size / MIB)
        return size
