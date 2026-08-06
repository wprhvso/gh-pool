from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Generator
from typing import TYPE_CHECKING, Any
from uuid import UUID

from gh_chrome_protocol import CommandArgs

if TYPE_CHECKING:
    from gh_chrome.session import Session


class Command[T]:
    __slots__ = ("_command_id", "_future", "_seq", "_task")

    def __init__(
        self, session: Session, args: CommandArgs | Awaitable[CommandArgs], timeout: float | None
    ) -> None:
        self._future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._command_id: UUID | None = None
        self._seq: int | None = None
        self._task = asyncio.create_task(session._submit(self, args, timeout))

    @property
    def command_id(self) -> UUID | None:
        return self._command_id

    @property
    def seq(self) -> int | None:
        return self._seq

    def done(self) -> bool:
        return self._future.done()

    async def wait(self, timeout: float | None = None) -> T:
        if timeout is None:
            return await self._future
        async with asyncio.timeout(timeout):
            return await asyncio.shield(self._future)

    def __await__(self) -> Generator[Any, None, T]:
        return self.wait().__await__()

    def _accepted(self, command_id: UUID, seq: int) -> None:
        self._command_id = command_id
        self._seq = seq

    def _resolve(self, result: Any) -> None:
        if not self._future.done():
            self._future.set_result(result)

    def _fail(self, error: BaseException) -> None:
        if not self._future.done():
            self._future.set_exception(error)
