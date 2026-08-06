from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable
from uuid import UUID

from gh_chrome_protocol import Event
from gh_chrome_protocol.sse import parse_sse

from gh_chrome_client.http import Http

log = logging.getLogger(__name__)

MIN_BACKOFF = 0.5
MAX_BACKOFF = 8.0


class EventStream:
    def __init__(self, http: Http, session_id: UUID, on_event: Callable[[Event], None]) -> None:
        self._http = http
        self._session_id = session_id
        self._on_event = on_event
        self._task: asyncio.Task[None] | None = None
        self._last_seq = 0
        self._stopped = False

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        backoff = MIN_BACKOFF
        while not self._stopped:
            try:
                await self._consume()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("event stream dropped: %s", exc)
                jitter = 0.5 + secrets.randbelow(1000) / 1000
                await asyncio.sleep(backoff * jitter)
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _consume(self) -> None:
        async with self._http.events(self._session_id, self._last_seq) as chunks:
            async for message in parse_sse(chunks):
                event = Event.model_validate_json(message.data)
                self._last_seq = event.seq
                self._on_event(event)
