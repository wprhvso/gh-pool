import asyncio
from contextlib import asynccontextmanager
from typing import cast
from uuid import uuid4

from gh_chrome_protocol import Bare, CommandEnvelope, CommandError, ErrorCode, Method
from gh_chrome_runner.http import ServerClient
from gh_chrome_runner.loop import Runner

CLOSE = b"event: close\ndata: {}\n\n"


def _command(method: Method = Method.TITLE, timeout_ms: int = 30_000) -> bytes:
    envelope = CommandEnvelope(
        command_id=uuid4(),
        seq=1,
        args=Bare(method=method),
        timeout_ms=timeout_ms,
    )
    return f"event: command\ndata: {envelope.model_dump_json()}\n\n".encode()


class FakeServer:
    """The runner's side of the wire, with the server replaced by a script."""

    def __init__(self, *frames: bytes) -> None:
        self._frames = frames
        self.completed: list[tuple[object, ErrorCode | None]] = []

    @asynccontextmanager
    async def stream(self):
        async def chunks():
            for frame in self._frames:
                yield frame

        yield chunks()

    async def complete(self, _command_id, result=None, error=None) -> None:
        self.completed.append((result, None if error is None else error.code))


class FakeActions:
    """Enough of the actions for the loop: one that answers and one that does not."""

    def __init__(self, *, block: bool = False) -> None:
        self._block = block

    async def dispatch(self, _args) -> str:
        if self._block:
            await asyncio.Event().wait()
        return "a page"

    def to_error(self, exc: Exception) -> CommandError:
        code = (
            ErrorCode.TIMEOUT
            if isinstance(exc, TimeoutError)
            else ErrorCode.RUNNER_ERROR
        )
        return CommandError(code=code, message=str(exc))


def _runner(server: FakeServer) -> Runner:
    runner = Runner(uuid4())
    runner._server = cast("ServerClient", server)
    return runner


async def _drain(runner: Runner, actions: FakeActions) -> bool:
    told = await runner._consume(actions, lambda: True)
    await runner._await_current()
    return told


async def test_a_close_frame_is_the_server_asking():
    runner = _runner(FakeServer(CLOSE))

    assert await _drain(runner, FakeActions()) is True


async def test_a_stream_that_just_ends_is_not_the_server_asking():
    """A restart, a proxy giving up: neither says anything about the session.

    Confirming a close on the strength of one used to end a session the client
    was still using, and told the client all its work had finished.
    """
    server = FakeServer(_command())
    runner = _runner(server)

    told = await _drain(runner, FakeActions())

    assert told is False
    assert server.completed == [("a page", None)]


async def test_a_command_is_bounded_by_the_timeout_it_carries():
    """The waits inside the actions poll for as long as it takes.

    The server's cancel used to be the only thing that bounded them, and it
    cannot arrive while the runner is busy with the command it would cancel.
    """
    server = FakeServer(_command(timeout_ms=200))
    runner = _runner(server)

    async with asyncio.timeout(10):
        await _drain(runner, FakeActions(block=True))

    assert server.completed == [(None, ErrorCode.TIMEOUT)]
