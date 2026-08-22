from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from tests.chrome.e2e.stack import Background

from pool.protocol import CommandError, ErrorCode, SessionParams, TabOpened
from pool.browser.config import settings
from pool.browser.http import ServerClient

SESSION = uuid4()
TOKEN = "the-token-this-session-was-given"
PAYLOAD = b"a payload the runner carries" * 100


class Recorder:
    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.heartbeats = 0
        self.session_is_live = True
        self.served: Path | None = None
        self.served_as: str | None = None
        self.has_profile = True
        self.credentials: list[str] = []


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def server(recorder: Recorder, tmp_path: Path) -> Iterator[Background]:
    async def config(request: Request) -> Response:
        recorder.credentials.append(request.headers.get("authorization", ""))
        return JSONResponse(
            {
                "session_id": str(SESSION),
                "params": SessionParams(width=1280, height=720).model_dump(mode="json"),
                "profile": "a-profile",
                "persist": True,
                "has_profile_archive": True,
                "segment_seconds": 2.0,
            }
        )

    async def heartbeat(_request: Request) -> Response:
        recorder.heartbeats += 1
        if recorder.session_is_live:
            return Response(status_code=204)
        return JSONResponse({"detail": "session is closed"}, status_code=409)

    async def complete(request: Request) -> Response:
        recorder.seen.append(await request.json())
        return Response(status_code=204)

    async def event(request: Request) -> Response:
        recorder.seen.append(await request.json())
        return Response(status_code=204)

    async def closed(_request: Request) -> Response:
        recorder.seen.append({"closed": True})
        return Response(status_code=204)

    async def put_anything(request: Request) -> Response:
        body = b""
        async for chunk in request.stream():
            body += chunk
        recorder.seen.append({"put": request.url.path, "size": len(body)})
        return Response(status_code=204)

    async def upload(_request: Request) -> Response:
        if recorder.served is None:
            return Response(status_code=404)
        return FileResponse(recorder.served, filename=recorder.served_as)

    async def profile(_request: Request) -> Response:
        if not recorder.has_profile:
            return Response(status_code=404)
        return Response(PAYLOAD)

    running = Background(
        Starlette(
            routes=[
                Route(f"/runner/{SESSION}/config", config),
                Route(f"/runner/{SESSION}/heartbeat", heartbeat, methods=["POST"]),
                Route(
                    "/runner/{session_id}/commands/{command_id}",
                    complete,
                    methods=["POST"],
                ),
                Route(f"/runner/{SESSION}/events", event, methods=["POST"]),
                Route(f"/runner/{SESSION}/closed", closed, methods=["POST"]),
                Route(f"/runner/{SESSION}/profile", profile, methods=["GET"]),
                Route(f"/runner/{SESSION}/files/{{file_id}}", upload, methods=["GET"]),
                Route(
                    "/runner/{session_id}/{kind:path}", put_anything, methods=["PUT"]
                ),
            ]
        )
    )
    running.start()
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
async def client(
    server: Background, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ServerClient]:
    monkeypatch.setattr(settings, "url", server.url)
    monkeypatch.setattr(settings, "token", TOKEN)
    running = ServerClient(SESSION)
    try:
        yield running
    finally:
        await running.aclose()


async def test_the_runner_reads_the_configuration_of_its_own_session(
    client: ServerClient,
):
    config = await client.config()

    assert config.session_id == SESSION
    assert config.params.width == 1280
    assert config.profile == "a-profile"
    assert config.segment_seconds == 2.0


async def test_a_heartbeat_is_true_while_the_server_still_wants_the_runner(
    client: ServerClient, recorder: Recorder
):
    assert await client.heartbeat() is True
    assert recorder.heartbeats == 1


async def test_a_heartbeat_the_server_refuses_says_the_session_is_over(
    client: ServerClient, recorder: Recorder
):
    recorder.session_is_live = False

    assert await client.heartbeat() is False


async def test_a_result_is_handed_back_under_the_command_it_answers(
    client: ServerClient, recorder: Recorder
):
    command_id = uuid4()

    await client.complete(command_id, "the page title")

    assert recorder.seen[-1] == {
        "command_id": str(command_id),
        "result": "the page title",
        "error": None,
    }


async def test_a_failure_is_handed_back_with_the_code_that_names_it(
    client: ServerClient, recorder: Recorder
):
    command_id = uuid4()
    error = CommandError(code=ErrorCode.NOT_FOUND, message="#nope was never there")

    await client.complete(command_id, None, error)

    assert recorder.seen[-1]["error"] == {
        "code": "not_found",
        "message": "#nope was never there",
    }


async def test_an_announcement_travels_as_the_event_it_is(
    client: ServerClient, recorder: Recorder
):
    await client.event(TabOpened(index=1, url="https://example.com/", active=True))

    assert recorder.seen[-1] == {
        "data": {
            "type": "tab_opened",
            "index": 1,
            "url": "https://example.com/",
            "active": True,
        }
    }


async def test_the_runner_confirms_the_close_it_was_asked_for(
    client: ServerClient, recorder: Recorder
):
    await client.confirm_close()

    assert recorder.seen[-1] == {"closed": True}


async def test_a_file_is_put_where_it_was_addressed(
    client: ServerClient, recorder: Recorder, tmp_path: Path
):
    source = tmp_path / "segment.m4s"
    source.write_bytes(PAYLOAD)

    await client.put_file("segments/12", source)

    assert recorder.seen[-1] == {
        "put": f"/runner/{SESSION}/segments/12",
        "size": len(PAYLOAD),
    }


async def test_a_profile_archive_is_written_where_it_was_asked_for(
    client: ServerClient, tmp_path: Path
):
    target = tmp_path / "restored" / "profile.tar.zst"

    assert await client.get_profile(target) is True
    assert target.read_bytes() == PAYLOAD


async def test_a_session_with_no_archive_yet_is_not_a_failure(
    client: ServerClient, recorder: Recorder, tmp_path: Path
):
    recorder.has_profile = False

    assert await client.get_profile(tmp_path / "profile.tar.zst") is False


@pytest.mark.parametrize(
    "name",
    [
        "payload.bin",
        "quarterly report.csv",
        "отчёт.pdf",
        "a,b;c.txt",
        "100% done.txt",
    ],
)
async def test_an_upload_reaches_the_runner_under_the_name_it_was_given(
    client: ServerClient, recorder: Recorder, tmp_path: Path, name: str
):
    source = tmp_path / "stored.bin"
    source.write_bytes(PAYLOAD)
    recorder.served = source
    recorder.served_as = name
    file_id = str(uuid4())

    fetched = await client.get_upload(file_id, tmp_path / "uploads")

    assert fetched.name == name
    assert fetched.parent.name == file_id
    assert fetched.read_bytes() == PAYLOAD


async def test_an_upload_the_server_does_not_name_falls_back_to_its_id(
    client: ServerClient, recorder: Recorder, tmp_path: Path
):
    source = tmp_path / "stored.bin"
    source.write_bytes(PAYLOAD)
    recorder.served = source
    recorder.served_as = None
    file_id = str(uuid4())

    fetched = await client.get_upload(file_id, tmp_path / "uploads")

    assert fetched.name == file_id


async def test_an_upload_that_is_not_there_is_reported_rather_than_written(
    client: ServerClient, tmp_path: Path
):
    with pytest.raises(httpx.HTTPStatusError, match="404"):
        await client.get_upload(str(uuid4()), tmp_path / "uploads")


async def test_every_request_carries_the_token_of_its_own_session(
    client: ServerClient, recorder: Recorder
):
    await client.config()

    assert recorder.credentials == [f"Bearer {TOKEN}"]
