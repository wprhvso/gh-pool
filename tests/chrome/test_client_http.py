from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from gh_pool.client.errors import (
    GhChromeError,
    Rejected,
    SessionUnavailable,
    TooManySessions,
)
from gh_pool.client.http import Http, _check
from gh_pool.protocol import (
    Bare,
    Method,
    SessionCreate,
    SessionStatus,
)
from tests.chrome.e2e.stack import Background

TOKEN = "a-shared-secret"
SESSION = UUID("2f1c9f38-6d0f-4a63-9e33-2f8a3f3f6b21")
PAYLOAD = b"a download the page made" * 30


class Recorder:
    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.credentials: list[str] = []
        self.answer_with: int | None = None


def _state(status: str = "pending") -> dict[str, Any]:
    return {
        "id": str(SESSION),
        "status": status,
        "state_stale": False,
        "profile": None,
        "persist": True,
        "params": {},
        "last_seq": 0,
    }


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def server(recorder: Recorder) -> Iterator[Background]:
    async def create(request: Request) -> Response:
        recorder.credentials.append(request.headers.get("authorization", ""))
        recorder.seen.append(await request.json())
        if recorder.answer_with is not None:
            return JSONResponse({"detail": "no"}, status_code=recorder.answer_with)
        return JSONResponse(_state(), status_code=201)

    async def read(_request: Request) -> Response:
        return JSONResponse(_state("closed"))

    async def commands(request: Request) -> Response:
        recorder.seen.append(await request.json())
        return JSONResponse(
            {"command_id": str(uuid4()), "seq": len(recorder.seen)}, status_code=202
        )

    async def close(_request: Request) -> Response:
        recorder.seen.append({"closed": True})
        return Response(status_code=204)

    async def files(request: Request) -> Response:
        form = await request.form()
        uploaded = form["file"]
        recorder.seen.append({"filename": getattr(uploaded, "filename", None)})
        return JSONResponse({"file_id": str(uuid4())}, status_code=201)

    async def download(request: Request) -> Response:
        recorder.seen.append({"asked_for": request.path_params["name"]})
        if request.path_params["name"] == "missing.bin":
            return JSONResponse({"detail": "unknown download"}, status_code=404)
        return Response(PAYLOAD)

    async def profiles(_request: Request) -> Response:
        return JSONResponse(
            [{"name": "work", "size": 12, "stale": False, "updated_at": None}]
        )

    async def events(request: Request) -> Response:
        recorder.seen.append(
            {
                "last_seq": request.query_params.get("last_seq"),
                "resume": request.headers.get("last-event-id"),
            }
        )

        async def frames() -> AsyncIterator[bytes]:
            yield b'event: session_ready\ndata: {"seq": 1, "data": '
            yield b'{"type": "session_ready", "state_stale": false}}\n\n'

        return StreamingResponse(frames(), media_type="text/event-stream")

    running = Background(
        Starlette(
            routes=[
                Route("/sessions", create, methods=["POST"]),
                Route(f"/sessions/{SESSION}", read, methods=["GET"]),
                Route(f"/sessions/{SESSION}/commands", commands, methods=["POST"]),
                Route(f"/sessions/{SESSION}/close", close, methods=["POST"]),
                Route(f"/sessions/{SESSION}/files", files, methods=["POST"]),
                Route(f"/sessions/{SESSION}/downloads/{{name}}", download),
                Route(f"/sessions/{SESSION}/events", events),
                Route("/profiles", profiles),
            ]
        )
    )
    running.start()
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
async def http(server: Background) -> AsyncIterator[Http]:
    client = Http(server.url, TOKEN)
    try:
        yield client
    finally:
        await client.aclose()


def _response(code: int, body: str = "no") -> httpx.Response:
    return httpx.Response(code, text=body)


def test_a_client_without_a_token_refuses_to_start(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GH_POOL_TOKEN", raising=False)

    with pytest.raises(GhChromeError, match="GH_POOL_TOKEN"):
        Http("http://127.0.0.1:1")


async def test_the_server_and_the_token_are_taken_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, server: Background, recorder: Recorder
):
    monkeypatch.setenv("GH_POOL_SERVER", f"{server.url}/")
    monkeypatch.setenv("GH_POOL_TOKEN", TOKEN)
    client = Http()

    try:
        await client.create_session(SessionCreate())
    finally:
        await client.aclose()

    assert client.base_url == server.url
    assert recorder.credentials == [f"Bearer {TOKEN}"]


async def test_a_session_is_asked_for_with_what_the_caller_wanted(
    http: Http, recorder: Recorder
):
    state = await http.create_session(SessionCreate(profile="work", persist=False))

    assert state.id == SESSION
    assert state.status is SessionStatus.PENDING
    assert recorder.seen[0]["profile"] == "work"
    assert recorder.seen[0]["persist"] is False


async def test_the_state_of_a_session_can_be_asked_for(http: Http):
    state = await http.get_session(SESSION)

    assert state.status is SessionStatus.CLOSED


async def test_a_command_is_accepted_with_the_number_it_was_given(http: Http):
    accepted = await http.enqueue(SESSION, Bare(method=Method.TITLE), timeout=5.0)

    assert accepted.seq == 1


@pytest.mark.parametrize(
    ("code", "error"), [(429, TooManySessions), (409, SessionUnavailable)]
)
async def test_a_session_the_server_will_not_make_is_reported_as_such(
    http: Http, recorder: Recorder, code: int, error: type[Exception]
):
    recorder.answer_with = code

    with pytest.raises(error):
        await http.create_session(SessionCreate())


@pytest.mark.parametrize("code", [401, 403, 404, 410])
async def test_an_answer_that_will_not_change_is_final(
    http: Http, recorder: Recorder, code: int
):
    recorder.answer_with = code

    with pytest.raises(Rejected) as refused:
        await http.create_session(SessionCreate())

    assert refused.value.status == code


@pytest.mark.parametrize("code", [500, 502, 503])
async def test_a_server_that_is_having_trouble_is_reported_plainly(
    http: Http, recorder: Recorder, code: int
):
    recorder.answer_with = code

    with pytest.raises(GhChromeError, match=str(code)):
        await http.create_session(SessionCreate())


def test_the_status_a_server_answered_decides_the_exception():
    assert _check(_response(200)).status_code == 200
    with pytest.raises(TooManySessions):
        _check(_response(429))
    with pytest.raises(SessionUnavailable):
        _check(_response(409))
    with pytest.raises(Rejected):
        _check(_response(404))
    with pytest.raises(GhChromeError):
        _check(_response(500))


async def test_closing_a_session_that_is_already_over_is_not_an_error(http: Http):
    await http.close_session(SESSION)


async def test_a_file_is_uploaded_under_its_own_name(
    http: Http, recorder: Recorder, tmp_path: Path
):
    source = tmp_path / "payload.bin"
    source.write_bytes(PAYLOAD)

    await http.upload_file(SESSION, source)

    assert recorder.seen[-1] == {"filename": "payload.bin"}


async def test_a_download_is_written_where_the_caller_asked_for_it(
    http: Http, tmp_path: Path
):
    target = tmp_path / "kept" / "report.bin"

    written = await http.download(SESSION, "report.bin", target)

    assert written == target
    assert target.read_bytes() == PAYLOAD


async def test_a_download_named_by_the_site_is_asked_for_exactly(
    http: Http, recorder: Recorder, tmp_path: Path
):
    await http.download(SESSION, "a report #1?v=2.bin", tmp_path / "report.bin")

    assert recorder.seen[-1] == {"asked_for": "a report #1?v=2.bin"}


async def test_a_download_that_is_not_there_is_reported_before_anything_is_written(
    http: Http, tmp_path: Path
):
    target = tmp_path / "missing.bin"

    with pytest.raises(Rejected):
        await http.download(SESSION, "missing.bin", target)

    assert not target.exists()


async def test_the_profiles_the_server_keeps_are_listed(http: Http):
    stored = await http.profiles()

    assert [profile.name for profile in stored] == ["work"]
    assert stored[0].size == 12


async def test_the_event_stream_is_asked_for_from_where_the_reader_left_off(
    http: Http, recorder: Recorder
):
    async with http.events(SESSION, 3) as chunks:
        body = b"".join([chunk async for chunk in chunks])

    assert b"session_ready" in body
    assert recorder.seen[-1] == {"last_seq": "3", "resume": "3"}
