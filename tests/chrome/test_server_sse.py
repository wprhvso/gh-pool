import asyncio
from collections.abc import AsyncGenerator

import pytest
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.requests import Request

from gh_chrome_protocol import SessionReady, TabOpened
from gh_chrome_server import sse


class Payload(BaseModel):
    said: str


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = Headers(headers or {}).raw
    return Request({"type": "http", "method": "GET", "headers": raw, "path": "/"})


def _text(chunk: str | bytes | memoryview[int]) -> str:
    return chunk if isinstance(chunk, str) else bytes(chunk).decode()


async def _collect(source: AsyncGenerator[sse.Frame], count: int) -> list[str]:
    body = sse._pump(source)
    seen: list[str] = []
    try:
        async with asyncio.timeout(5):
            async for chunk in body:
                seen.append(chunk)
                if len(seen) >= count:
                    break
    finally:
        await body.aclose()
    return seen


async def _frames(*items: sse.Frame) -> AsyncGenerator[sse.Frame]:
    for item in items:
        yield item


def test_a_stream_resumes_from_the_number_the_reader_last_saw():
    assert sse.resume_from(_request({"last-event-id": "12"}), 0) == 12


def test_a_stream_without_a_header_resumes_from_what_the_caller_asked_for():
    assert sse.resume_from(_request(), 7) == 7


@pytest.mark.parametrize("offered", ["", "nonsense", "12.5", "twelve"])
def test_a_resume_point_that_is_not_a_number_falls_back(offered: str):
    assert sse.resume_from(_request({"last-event-id": offered}), 3) == 3


async def test_a_frame_carries_its_name_its_body_and_its_number():
    chunks = await _collect(
        _frames(sse.Frame(name="tab_opened", data=Payload(said="hello"), id=4)), 2
    )

    assert chunks[0] == f"retry: {sse.RETRY_MS}\n\n"
    assert chunks[1] == 'id: 4\nevent: tab_opened\ndata: {"said":"hello"}\n\n'


async def test_a_frame_without_a_number_carries_none():
    chunks = await _collect(
        _frames(sse.Frame(name="cancel", data=Payload(said="x"))), 2
    )

    assert chunks[1].startswith("event: cancel\n")


async def test_the_body_of_a_frame_is_one_line_of_json():
    frame = sse.Frame(name="session_ready", data=SessionReady(state_stale=True))

    chunks = await _collect(_frames(frame), 2)

    assert (
        chunks[1]
        == 'event: session_ready\ndata: {"type":"session_ready","state_stale":true}\n\n'
    )


async def test_every_frame_of_a_stream_arrives_in_order():
    frames = _frames(
        sse.Frame(name="a", data=Payload(said="first"), id=1),
        sse.Frame(name="b", data=Payload(said="second"), id=2),
    )

    chunks = await _collect(frames, 3)

    assert [chunk.splitlines()[0] for chunk in chunks[1:]] == ["id: 1", "id: 2"]


async def test_a_stream_that_says_nothing_still_keeps_the_connection_open(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(sse, "KEEPALIVE", 0.01)
    quiet: asyncio.Queue[sse.Frame] = asyncio.Queue()

    async def waiting() -> AsyncGenerator[sse.Frame]:
        yield await quiet.get()

    chunks = await _collect(waiting(), 3)

    assert chunks[1] == ": ping\n\n"
    assert chunks[2] == ": ping\n\n"


async def test_a_stream_that_ends_ends_the_response():
    response = sse.sse_response(_frames(sse.Frame(name="a", data=Payload(said="x"))))

    async with asyncio.timeout(5):
        seen = [_text(chunk) async for chunk in response.body_iterator]

    assert len(seen) == 2
    assert "event: a" in seen[1]


async def test_the_source_is_closed_when_the_reader_goes_away():
    closed = asyncio.Event()

    async def source() -> AsyncGenerator[sse.Frame]:
        try:
            yield sse.Frame(name="a", data=Payload(said="x"))
            await asyncio.Event().wait()
        finally:
            closed.set()

    body = sse._pump(source())
    async with asyncio.timeout(5):
        await anext(body)
        await anext(body)
        await body.aclose()

    assert closed.is_set()


async def test_the_response_tells_every_proxy_not_to_buffer_it():
    response = sse.sse_response(_frames())

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


async def test_an_event_of_the_protocol_is_named_by_its_own_type():
    announced = TabOpened(index=1, url="https://example.com/", active=True)

    chunks = await _collect(
        _frames(sse.Frame(name=str(announced.type), data=announced, id=9)), 2
    )

    assert chunks[1].startswith("id: 9\nevent: tab_opened\n")
