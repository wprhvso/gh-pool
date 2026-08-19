import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tests.e2e.stack import Background
from websockets.asyncio.server import ServerConnection, serve

from gh_chrome_runner.cdp import Cdp, CdpError


async def _serve(connection: ServerConnection) -> None:
    async for raw in connection:
        message = json.loads(raw)
        method = message["method"]
        if method == "silent":
            continue
        if method == "boom":
            answer: dict[str, Any] = {
                "id": message["id"],
                "error": {"code": -32000, "message": "it went wrong"},
            }
        elif method == "announce":
            await connection.send(
                json.dumps({"method": "Target.attachedToTarget", "params": {"a": 1}})
            )
            answer = {"id": message["id"], "result": {}}
        else:
            answer = {
                "id": message["id"],
                "result": {
                    "echo": message.get("params"),
                    "session": message.get("sessionId"),
                },
            }
        await connection.send(json.dumps(answer))


@pytest.fixture
async def cdp() -> AsyncIterator[Cdp]:
    async with serve(_serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = Cdp(f"ws://127.0.0.1:{port}")
        await client.connect()
        try:
            yield client
        finally:
            await client.close()


async def test_a_request_is_answered_with_its_own_result(cdp: Cdp):
    answer = await cdp.send("Page.navigate", {"url": "https://example.com/"})

    assert answer["echo"] == {"url": "https://example.com/"}


async def test_a_request_carries_the_page_it_belongs_to(cdp: Cdp):
    answer = await cdp.send("Runtime.evaluate", {"expression": "1"}, "s-1")

    assert answer["session"] == "s-1"


async def test_every_request_gets_its_own_answer(cdp: Cdp):
    answers = await asyncio.gather(
        *(cdp.send("Page.navigate", {"n": number}) for number in range(5))
    )

    assert [answer["echo"]["n"] for answer in answers] == list(range(5))


async def test_a_request_the_browser_refused_is_raised_where_it_was_made(cdp: Cdp):
    with pytest.raises(CdpError, match="it went wrong"):
        await cdp.send("boom")


async def test_what_the_browser_announces_reaches_whoever_asked_to_hear_it(cdp: Cdp):
    heard: list[dict[str, Any]] = []
    cdp.on("Target.attachedToTarget", heard.append)

    await cdp.send("announce")
    await asyncio.sleep(0.05)

    assert heard[0]["params"] == {"a": 1}


async def test_a_listener_that_was_taken_off_hears_nothing_more(cdp: Cdp):
    heard: list[dict[str, Any]] = []
    cdp.on("Target.attachedToTarget", heard.append)
    cdp.off("Target.attachedToTarget")

    await cdp.send("announce")
    await asyncio.sleep(0.05)

    assert heard == []


async def test_one_listener_that_fails_does_not_silence_the_others(cdp: Cdp):
    heard: list[dict[str, Any]] = []

    def failing(_message: dict[str, Any]) -> None:
        raise RuntimeError("this listener is broken")

    cdp.on("Target.attachedToTarget", failing)
    cdp.on("Target.attachedToTarget", heard.append)

    await cdp.send("announce")
    await asyncio.sleep(0.05)

    assert len(heard) == 1


async def test_a_request_made_before_the_browser_is_there_says_so():
    with pytest.raises(CdpError, match="not connected"):
        await Cdp("ws://127.0.0.1:1").send("Page.navigate")


async def test_a_request_nobody_will_ever_answer_ends_when_the_browser_does(cdp: Cdp):
    pending = asyncio.ensure_future(cdp.send("silent"))
    await asyncio.sleep(0.05)

    await cdp.close()

    with pytest.raises(CdpError, match="browser"):
        await pending


async def test_the_browser_going_away_ends_what_was_waiting():
    async with serve(_serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = Cdp(f"ws://127.0.0.1:{port}")
        await client.connect()
        pending = asyncio.ensure_future(client.send("silent"))
        await asyncio.sleep(0.05)
        server.close()

    with pytest.raises(CdpError):
        async with asyncio.timeout(5):
            await pending
    with contextlib.suppress(Exception):
        await client.close()


@pytest.fixture
def endpoint() -> Iterator[Background]:
    async def version(_request: Request) -> Response:
        return JSONResponse({"webSocketDebuggerUrl": "ws://127.0.0.1:0/devtools"})

    async def listing(_request: Request) -> Response:
        return JSONResponse(
            [
                {"id": "t1", "type": "page", "url": "https://example.com/"},
                {"id": "w1", "type": "service_worker", "url": "https://example.com/sw"},
            ]
        )

    running = Background(
        Starlette(
            routes=[
                Route("/json/version", version),
                Route("/json/list", listing),
            ]
        )
    )
    running.start()
    try:
        yield running
    finally:
        running.stop()


async def test_the_debugging_endpoint_is_read_from_the_browser(endpoint: Background):
    answer = await Cdp.version(endpoint.port)

    assert answer["webSocketDebuggerUrl"].startswith("ws://")


async def test_only_the_pages_count_as_pages(endpoint: Background):
    pages = await Cdp.pages(endpoint.port)

    assert [page["id"] for page in pages] == ["t1"]
