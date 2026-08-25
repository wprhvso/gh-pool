import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from gh_pool.client.errors import (
    ElementNotFound,
    Rejected,
    SessionDead,
    SessionNotReady,
)
from gh_pool.client.session import Session
from gh_pool.protocol import (
    CommandAccepted,
    CommandArgs,
    CommandError,
    CommandFailed,
    CommandFinished,
    Download,
    ErrorCode,
    Event,
    EventData,
    SessionClosed,
    SessionParams,
    SessionReady,
    SessionState,
    SessionStatus,
    TabOpened,
)

CLOSE_TIMEOUT = 0.0


def _state(status: SessionStatus = SessionStatus.PENDING) -> SessionState:
    return SessionState(
        id=uuid4(),
        status=status,
        state_stale=False,
        profile=None,
        persist=True,
        params=SessionParams(),
        last_seq=0,
    )


class FakeHttp:
    def __init__(self, state: SessionState) -> None:
        self.base_url = "http://chrome.example.com"
        self.state = state
        self.enqueued: list[CommandArgs] = []
        self.accepted: list[UUID] = []
        self.resumed_from: list[int] = []
        self.close_requests = 0
        self.acloses = 0
        self.enqueue_error: Exception | None = None
        self.stream_error: Exception | None = None
        self.held: asyncio.Event | None = None
        self.uploaded: list[Path] = []
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._seq = 0

    def feed(self, data: EventData) -> Event:
        self._seq += 1
        event = Event(seq=self._seq, data=data)
        self.feed_raw(
            f"id: {event.seq}\nevent: {data.type}\ndata: {event.model_dump_json()}\n\n".encode()
        )
        return event

    def feed_raw(self, chunk: bytes) -> None:
        self._frames.put_nowait(chunk)

    def feed_unreadable(self) -> int:
        self._seq += 1
        payload = f'{{"seq": {self._seq}, "data": {{"type": "teleported"}}}}'
        self.feed_raw(
            f"id: {self._seq}\nevent: teleported\ndata: {payload}\n\n".encode()
        )
        return self._seq

    def end_stream(self) -> None:
        self._frames.put_nowait(None)

    @asynccontextmanager
    async def events(
        self, _session_id: UUID, last_seq: int
    ) -> AsyncGenerator[AsyncIterator[bytes]]:
        self.resumed_from.append(last_seq)
        if self.stream_error is not None:
            failure, self.stream_error = self.stream_error, None
            raise failure
        yield self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        while (chunk := await self._frames.get()) is not None:
            yield chunk

    async def get_session(self, _session_id: UUID) -> SessionState:
        return self.state

    async def enqueue(
        self, _session_id: UUID, args: CommandArgs, _timeout: float | None
    ) -> CommandAccepted:
        if self.held is not None:
            await self.held.wait()
        if self.enqueue_error is not None:
            raise self.enqueue_error
        self.enqueued.append(args)
        command_id = self.accepted.pop(0) if self.accepted else uuid4()
        return CommandAccepted(command_id=command_id, seq=len(self.enqueued))

    async def close_session(self, _session_id: UUID) -> None:
        self.close_requests += 1

    async def upload_file(self, _session_id: UUID, path: Path) -> UUID:
        self.uploaded.append(path)
        return uuid4()

    async def aclose(self) -> None:
        self.acloses += 1


async def _until(condition, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise TimeoutError(f"{what} did not happen in {timeout}s")
        await asyncio.sleep(0.01)


@pytest.fixture
async def session() -> AsyncIterator[tuple[Session, FakeHttp]]:
    state = _state()
    http = FakeHttp(state)
    running = Session(http, state, CLOSE_TIMEOUT)
    try:
        yield running, http
    finally:
        http.end_stream()
        await running.close()


async def test_a_session_is_ready_once_the_runner_says_so(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    http.feed(SessionReady(state_stale=True))

    await running.ready(timeout=5)

    assert running.alive
    assert running.state_stale


async def test_a_session_that_ends_before_the_runner_arrives_is_dead(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    http.feed(SessionClosed(reason="dead"))

    with pytest.raises(SessionDead):
        await running.ready(timeout=5)


async def test_a_runner_that_never_arrives_is_reported_as_such(
    session: tuple[Session, FakeHttp],
):
    running, _ = session

    with pytest.raises(SessionNotReady):
        await running.ready(timeout=0.05)


async def test_a_command_settles_with_the_result_the_runner_sent(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    command_id = uuid4()
    http.accepted.append(command_id)

    command = running.title()
    await _until(lambda: bool(http.enqueued), "the command to be enqueued")
    http.feed(CommandFinished(command_id=command_id, result="a page"))

    assert await command.wait(timeout=5) == "a page"


async def test_a_command_that_failed_raises_the_exception_its_code_names(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    command_id = uuid4()
    http.accepted.append(command_id)

    command = running.click("#nope")
    await _until(lambda: bool(http.enqueued), "the command to be enqueued")
    http.feed(
        CommandFailed(
            command_id=command_id,
            error=CommandError(code=ErrorCode.NOT_FOUND, message="#nope"),
        )
    )

    with pytest.raises(ElementNotFound, match="#nope"):
        await command.wait(timeout=5)


async def test_an_answer_that_arrives_before_the_request_returns_is_not_lost(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    command_id = uuid4()
    http.accepted.append(command_id)
    http.held = asyncio.Event()

    command = running.title()
    http.feed(CommandFinished(command_id=command_id, result="answered early"))
    await _until(lambda: running._stash != {}, "the answer to be stashed")
    http.held.set()

    assert await command.wait(timeout=5) == "answered early"


async def test_a_command_is_not_cancelled_by_whoever_gave_up_waiting(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    command_id = uuid4()
    http.accepted.append(command_id)

    command = running.title()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(command.wait(timeout=5), 0.05)
    assert not command.done()

    http.feed(CommandFinished(command_id=command_id, result="late but mine"))

    assert await command.wait(timeout=5) == "late but mine"


async def test_the_watcher_hears_what_the_session_announces(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    watching = running.events()

    http.feed(TabOpened(index=1, url="https://example.com/", active=True))

    async with asyncio.timeout(5):
        event = await anext(watching)
    assert isinstance(event.data, TabOpened)
    assert event.data.index == 1


async def test_the_watcher_is_not_told_about_the_commands_it_did_not_ask_for(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    watching = running.events()
    command_id = uuid4()

    http.feed(CommandFinished(command_id=command_id, result="a page"))
    http.feed(Download(name="report.bin", size=4, url="http://example/report.bin"))

    async with asyncio.timeout(5):
        event = await anext(watching)
    assert isinstance(event.data, Download)


async def test_the_watcher_stops_when_the_session_does(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    watching = running.events()

    http.feed(SessionClosed(reason="closed"))

    async with asyncio.timeout(5):
        assert isinstance((await anext(watching)).data, SessionClosed)
        with pytest.raises(StopAsyncIteration):
            await anext(watching)


async def test_a_watcher_opened_after_the_end_is_not_left_waiting(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    http.feed(SessionClosed(reason="closed"))
    await _until(lambda: not running.alive, "the session to finish")

    watching = running.events()

    async with asyncio.timeout(5):
        with pytest.raises(StopAsyncIteration):
            await anext(watching)


async def test_an_event_this_client_cannot_read_does_not_wedge_the_stream(
    session: tuple[Session, FakeHttp],
):
    running, http = session

    skipped = http.feed_unreadable()
    http.feed(SessionReady(state_stale=False))

    await running.ready(timeout=5)
    assert running._last_seq > skipped


async def test_a_dropped_stream_is_picked_back_up_where_it_left_off(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    http.feed(SessionReady(state_stale=False))
    await running.ready(timeout=5)
    seen = running._last_seq

    http.stream_error = ConnectionError("the stream dropped")
    http.end_stream()

    await _until(lambda: len(http.resumed_from) >= 3, "the stream to be reopened")
    assert http.resumed_from[-1] == seen


async def test_a_stream_that_ends_while_the_session_lives_is_opened_again(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    http.state = _state(SessionStatus.ACTIVE)
    http.feed(SessionReady(state_stale=False))
    await running.ready(timeout=5)

    http.end_stream()

    await _until(lambda: len(http.resumed_from) >= 2, "the stream to be reopened")
    assert running.alive


async def test_a_stream_that_ends_because_the_session_did_settles_every_caller(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    http.state = _state(SessionStatus.CLOSED)
    command_id = uuid4()
    http.accepted.append(command_id)
    command = running.title()
    await _until(lambda: bool(http.enqueued), "the command to be enqueued")

    http.end_stream()

    with pytest.raises(SessionDead):
        await command.wait(timeout=5)
    assert not running.alive


async def test_a_stream_the_server_refuses_settles_every_caller(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    command_id = uuid4()
    http.accepted.append(command_id)
    command = running.title()
    await _until(lambda: bool(http.enqueued), "the command to be enqueued")

    http.stream_error = Rejected(404, "no such session")
    http.end_stream()

    with pytest.raises(SessionDead):
        await command.wait(timeout=5)
    assert not running.alive


async def test_closing_the_session_settles_what_was_still_pending(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    http.accepted.append(uuid4())
    command = running.title()
    await _until(lambda: bool(http.enqueued), "the command to be enqueued")

    await running.close()

    assert http.close_requests == 1
    assert http.acloses == 1
    assert not running.alive
    with pytest.raises(SessionDead):
        await command.wait(timeout=5)


async def test_closing_twice_asks_the_server_once(session: tuple[Session, FakeHttp]):
    running, http = session

    await running.close()
    await running.close()

    assert http.close_requests == 1


async def test_a_command_asked_for_after_the_close_never_reaches_the_server(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    await running.close()
    enqueued = len(http.enqueued)

    with pytest.raises(SessionDead):
        await running.title().wait(timeout=5)

    assert len(http.enqueued) == enqueued


async def test_detaching_leaves_the_session_running_on_the_server(
    session: tuple[Session, FakeHttp],
):
    running, http = session

    await running.detach()

    assert http.close_requests == 0
    assert http.acloses == 1
    assert running._reader.done()
    assert running.alive


async def test_detaching_settles_what_was_still_pending(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    http.accepted.append(uuid4())
    command = running.title()
    await _until(lambda: bool(http.enqueued), "the command to be enqueued")

    await running.detach()

    with pytest.raises(SessionDead):
        await command.wait(timeout=5)


async def test_detaching_stops_the_watcher(session: tuple[Session, FakeHttp]):
    running, _ = session
    watching = running.events()

    await running.detach()

    async with asyncio.timeout(5):
        with pytest.raises(StopAsyncIteration):
            await anext(watching)


async def test_a_watcher_opened_after_a_detach_is_not_left_waiting(
    session: tuple[Session, FakeHttp],
):
    running, _ = session
    await running.detach()

    watching = running.events()

    async with asyncio.timeout(5):
        with pytest.raises(StopAsyncIteration):
            await anext(watching)


async def test_a_command_asked_for_after_a_detach_never_reaches_the_server(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    await running.detach()
    enqueued = len(http.enqueued)

    with pytest.raises(SessionDead):
        await running.title().wait(timeout=5)

    assert len(http.enqueued) == enqueued


async def test_detaching_twice_lets_go_once(session: tuple[Session, FakeHttp]):
    running, http = session

    await running.detach()
    await running.detach()

    assert http.acloses == 1
    assert http.close_requests == 0


async def test_detaching_after_a_close_is_harmless(session: tuple[Session, FakeHttp]):
    running, http = session

    await running.close()
    await running.detach()

    assert http.close_requests == 1
    assert http.acloses == 1
    assert not running.alive


async def test_closing_after_a_detach_leaves_the_session_alone(
    session: tuple[Session, FakeHttp],
):
    running, http = session

    await running.detach()
    await running.close()

    assert http.close_requests == 0
    assert running.alive


async def test_a_refused_enqueue_reaches_the_caller_as_it_was(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    http.enqueue_error = Rejected(403, "not your session")

    with pytest.raises(Rejected):
        await running.title().wait(timeout=5)


async def test_an_upload_sends_the_file_before_the_command(
    session: tuple[Session, FakeHttp], tmp_path: Path
):
    running, http = session
    source = tmp_path / "payload.bin"
    source.write_bytes(b"something to upload")
    command_id = uuid4()
    http.accepted.append(command_id)

    command = running.upload("#pick", path=source)
    await _until(lambda: bool(http.enqueued), "the command to be enqueued")
    http.feed(CommandFinished(command_id=command_id, result=None))
    await command.wait(timeout=5)

    assert http.uploaded == [source]


def test_an_upload_needs_exactly_one_source(session: tuple[Session, FakeHttp]):
    running, _ = session

    with pytest.raises(ValueError, match="exactly one"):
        running.upload("#pick")
    with pytest.raises(ValueError, match="exactly one"):
        running.upload("#pick", path=Path("a"), url="http://example.com/b")


def test_the_player_is_where_the_server_says_it_is(session: tuple[Session, FakeHttp]):
    running, _ = session

    assert running.player_url == f"http://chrome.example.com/s/{running.id}"


def test_the_parameters_are_the_ones_the_session_was_made_with(
    session: tuple[Session, FakeHttp],
):
    running, _ = session

    assert running.params.width == SessionParams().width


async def test_the_events_a_client_watches_survive_a_reconnect(
    session: tuple[Session, FakeHttp],
):
    running, http = session
    watching = running.events()
    http.feed(SessionReady(state_stale=False))
    await running.ready(timeout=5)

    http.state = _state(SessionStatus.ACTIVE)
    http.end_stream()
    await _until(lambda: len(http.resumed_from) >= 2, "the stream to be reopened")
    http.feed(TabOpened(index=2, url="https://example.com/", active=True))

    async with asyncio.timeout(5):
        event = await anext(watching)
    assert isinstance(event.data, TabOpened)
