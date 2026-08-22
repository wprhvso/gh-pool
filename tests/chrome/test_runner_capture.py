from pathlib import Path
from uuid import uuid4

import pytest

from pool.protocol import RunnerConfig, SessionParams
from pool.runner import capture
from pool.runner.capture import Capture
from pool.runner.config import settings


class FakeDisplay:
    name = ":99"


class FakeServer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self.refuse: set[str] = set()

    async def put_file(self, path: str, source: Path) -> None:
        if path in self.refuse:
            raise ConnectionError(f"the server would not take {path}")
        self.sent.append((path, source.read_bytes()))

    @property
    def routes(self) -> list[str]:
        return [route for route, _ in self.sent]


def _config(**params: object) -> RunnerConfig:
    return RunnerConfig(
        session_id=uuid4(),
        params=SessionParams(**params),  # pyright: ignore[reportArgumentType]
        profile=None,
        persist=False,
        has_profile_archive=False,
        segment_seconds=1.5,
    )


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "workdir", tmp_path)
    settings.segments_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _capture(server: FakeServer, config: RunnerConfig | None = None) -> Capture:
    return Capture(FakeDisplay(), server, config or _config())  # pyright: ignore[reportArgumentType]


def _wrote(name: str, content: bytes = b"a recorded second") -> Path:
    path = settings.segments_dir / name
    path.write_bytes(content)
    return path


def test_the_recorder_is_told_the_screen_and_the_quality_it_was_asked_for():
    command = capture._ffmpeg_command(
        FakeDisplay(),  # pyright: ignore[reportArgumentType]
        _config(width=800, height=600, fps=5, bitrate="750k"),
    )

    assert "800x600" in command
    assert command[command.index("-framerate") + 1] == "5"
    assert command[command.index("-b:v") + 1] == "750k"
    assert command[command.index("-i") + 1] == ":99"
    assert command[command.index("-seg_duration") + 1] == "1.5"
    assert command[-1].endswith("out.mpd")


def test_the_recorder_keeps_the_keyframes_close_enough_to_seek_between():
    command = capture._ffmpeg_command(
        FakeDisplay(),  # pyright: ignore[reportArgumentType]
        _config(fps=10),
    )

    assert command[command.index("-g") + 1] == "20"


@pytest.mark.parametrize(
    ("name", "number"),
    [("chunk-stream0-1.m4s", "1"), ("chunk-stream0-000042.m4s", "000042")],
)
def test_a_segment_is_recognised_by_the_number_the_recorder_gave_it(
    name: str, number: str
):
    found = capture.SEGMENT_PATTERN.search(name)

    assert found is not None
    assert found.group(1) == number


@pytest.mark.parametrize(
    "name", ["init-stream0.m4s", "out.mpd", "chunk-stream1-1.m4s", "notes.txt"]
)
def test_whatever_is_not_a_segment_is_left_alone(name: str, workdir: Path):
    assert capture.SEGMENT_PATTERN.search(name) is None


async def test_a_segment_is_uploaded_once_it_stopped_growing(workdir: Path):
    server = FakeServer()
    recorder = _capture(server)
    _wrote(capture.INIT_NAME, b"the header")
    growing = _wrote("chunk-stream0-1.m4s", b"half")

    await recorder._scan(final=False)
    assert server.routes == ["init"]

    growing.write_bytes(b"the whole second")
    await recorder._scan(final=False)
    await recorder._scan(final=False)
    await recorder._scan(final=False)

    assert server.routes == ["init", "segments/1"]


async def test_a_segment_the_server_has_is_not_kept_on_the_runner(workdir: Path):
    server = FakeServer()
    recorder = _capture(server)
    _wrote(capture.INIT_NAME)
    path = _wrote("chunk-stream0-1.m4s")

    await recorder._scan(final=True)

    assert not path.exists()
    assert server.sent[-1] == ("segments/1", b"a recorded second")


async def test_a_segment_is_never_uploaded_twice(workdir: Path):
    server = FakeServer()
    recorder = _capture(server)
    _wrote(capture.INIT_NAME)
    _wrote("chunk-stream0-1.m4s")

    await recorder._scan(final=True)
    _wrote("chunk-stream0-1.m4s")
    await recorder._scan(final=True)

    assert server.routes.count("segments/1") == 1


async def test_the_last_seconds_are_taken_even_though_nothing_settled(workdir: Path):
    server = FakeServer()
    recorder = _capture(server)
    _wrote(capture.INIT_NAME)
    _wrote("chunk-stream0-1.m4s")
    _wrote("chunk-stream0-2.m4s")

    await recorder._scan(final=True)

    assert server.routes == ["init", "segments/1", "segments/2"]


async def test_an_empty_header_is_not_offered_as_one(workdir: Path):
    server = FakeServer()
    recorder = _capture(server)
    _wrote(capture.INIT_NAME, b"")

    await recorder._scan(final=True)

    assert server.routes == []


async def test_a_segment_the_server_would_not_take_is_tried_again_later(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(capture, "RETRIES", 1)
    server = FakeServer()
    server.refuse = {"segments/1"}
    recorder = _capture(server)
    _wrote(capture.INIT_NAME)
    path = _wrote("chunk-stream0-1.m4s")

    await recorder._scan(final=True)
    assert path.exists()

    server.refuse.clear()
    await recorder._scan(final=True)

    assert server.routes == ["init", "segments/1"]


async def test_a_recorder_that_restarted_keeps_numbering_where_it_left_off(
    workdir: Path,
):
    server = FakeServer()
    recorder = _capture(server)
    _wrote(capture.INIT_NAME)
    _wrote("chunk-stream0-1.m4s")
    _wrote("chunk-stream0-2.m4s")
    await recorder._scan(final=True)

    recorder._restart_numbering()
    _wrote("chunk-stream0-1.m4s", b"after the restart")
    await recorder._scan(final=True)

    assert server.routes == ["init", "segments/1", "segments/2", "segments/3"]
    assert server.sent[-1][1] == b"after the restart"


async def test_what_the_recorder_left_behind_is_dropped_when_it_restarts(
    workdir: Path,
):
    server = FakeServer()
    recorder = _capture(server)
    stale = _wrote("chunk-stream0-7.m4s")

    recorder._restart_numbering()

    assert not stale.exists()
