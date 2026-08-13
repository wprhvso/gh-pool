import asyncio
import contextlib

import pytest
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.typing import Subprotocol

from gh_chrome_protocol import tunnel
from gh_chrome_runner.tunnel import Link
from gh_chrome_server.tunnel import Tunnel as ServerTunnel
from gh_chrome_server.tunnel import TunnelDown, Tunnels

BODY = b"a live desktop" * 500


def test_a_frame_survives_a_round_trip():
    raw = tunnel.frame(tunnel.Op.DATA, 7, b"\x00\xffpayload")
    assert tunnel.parse(raw) == (tunnel.Op.DATA, 7, b"\x00\xffpayload")


def test_a_model_payload_is_encoded_as_json():
    raw = tunnel.frame(tunnel.Op.HEAD, 1, tunnel.Head(status=204))
    op, stream, payload = tunnel.parse(raw)
    assert (op, stream) == (tunnel.Op.HEAD, 1)
    assert tunnel.Head.model_validate_json(payload).status == 204


def test_a_truncated_frame_is_rejected():
    with pytest.raises(ValueError, match="truncated"):
        tunnel.parse(b"\x03\x00\x00")


class Pipe:
    def __init__(self):
        self.down = asyncio.Queue()
        self.up = asyncio.Queue()


class ServerEnd:
    def __init__(self, pipe):
        self._pipe = pipe

    async def send_bytes(self, data):
        await self._pipe.down.put(data)

    async def receive_bytes(self):
        item = await self._pipe.up.get()
        if item is None:
            raise WebSocketDisconnect(1000)
        return item

    async def close(self, _code=1000, _reason=""):
        await self._pipe.down.put(None)


class RunnerEnd:
    def __init__(self, pipe):
        self._pipe = pipe

    async def send(self, data):
        await self._pipe.up.put(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._pipe.down.get()
        if item is None:
            raise StopAsyncIteration
        return item


async def _echo(connection):
    async for message in connection:
        await connection.send(message)


def _missing():
    return Response(404, "Not Found", Headers([("Content-Length", "0")]))


def _http(_connection, request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        # KasmVNC drops the upgrade to a 404 unless both of these are present.
        wanted = ("Origin", "Sec-WebSocket-Protocol")
        if not request.path.startswith("/socket"):
            return _missing()
        return None if all(name in request.headers for name in wanted) else _missing()
    if request.path == "/" or request.path.startswith("/asset"):
        headers = Headers(
            [("Content-Type", "text/plain"), ("Content-Length", str(len(BODY)))]
        )
        return Response(200, "OK", headers, BODY)
    return _missing()


@contextlib.asynccontextmanager
async def _desktop():
    """A stand-in for KasmVNC: one port serving both files and the websocket."""
    async with serve(
        _echo,
        "127.0.0.1",
        0,
        process_request=_http,
        subprotocols=[Subprotocol("binary")],
    ) as server:
        yield server.sockets[0].getsockname()[1]


@contextlib.asynccontextmanager
async def _tunnel(port):
    pipe = Pipe()
    server = ServerTunnel(ServerEnd(pipe))
    link = Link(RunnerEnd(pipe), port)
    tasks = [asyncio.create_task(server.serve()), asyncio.create_task(link.pump())]
    try:
        yield server
    finally:
        await server.shutdown()
        await link.shutdown()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _drain(stream):
    return b"".join([chunk async for chunk in stream.body()])


async def _drain_frames(stream):
    while await stream.read() is not None:
        pass


async def test_a_page_is_fetched_through_the_tunnel():
    async with _desktop() as port, _tunnel(port) as server:
        stream = await server.open(
            tunnel.Open(kind=tunnel.Kind.HTTP, target="/asset.js")
        )
        await stream.send(tunnel.Op.EOF)
        head = await stream.head()
        assert head.status == 200
        assert ("content-type", "text/plain") in head.headers
        assert await _drain(stream) == BODY


async def test_an_unknown_page_keeps_its_status():
    async with _desktop() as port, _tunnel(port) as server:
        stream = await server.open(tunnel.Open(kind=tunnel.Kind.HTTP, target="/nope"))
        await stream.send(tunnel.Op.EOF)
        assert (await stream.head()).status == 404
        assert await _drain(stream) == b""


async def test_a_websocket_carries_frames_both_ways():
    async with _desktop() as port, _tunnel(port) as server:
        stream = await server.open(
            tunnel.Open(
                kind=tunnel.Kind.WS, target="/socket", subprotocols=[tunnel.BINARY]
            )
        )
        head = await stream.head()
        assert (head.status, head.subprotocol) == (101, tunnel.BINARY)
        await stream.send(tunnel.Op.DATA, b"\x00\x01\x02")
        assert await stream.read() == (tunnel.Op.DATA, b"\x00\x01\x02")
        await stream.send(tunnel.Op.TEXT, b"hello")
        assert await stream.read() == (tunnel.Op.TEXT, b"hello")
        await stream.close()


async def test_a_websocket_upgrade_carries_what_kasmvnc_demands():
    """No Origin or no subprotocol and KasmVNC answers 404 instead of upgrading."""
    async with _desktop() as port, _tunnel(port) as server:
        stream = await server.open(tunnel.Open(kind=tunnel.Kind.WS, target="/socket"))
        head = await stream.head()
        assert (head.status, head.subprotocol) == (101, tunnel.BINARY)


async def test_a_refused_websocket_comes_back_as_its_status():
    async with _desktop() as port, _tunnel(port) as server:
        stream = await server.open(tunnel.Open(kind=tunnel.Kind.WS, target="/gone"))
        assert (await stream.head()).status == 404


async def test_a_dead_desktop_fails_the_waiting_stream():
    async with _desktop() as port, _tunnel(port) as server:
        stream = await server.open(tunnel.Open(kind=tunnel.Kind.HTTP, target="/asset"))
        await server.shutdown()
        with pytest.raises(TunnelDown):
            await stream.head()


async def test_a_stream_nobody_drains_is_torn_down():
    pipe = Pipe()
    server = ServerTunnel(ServerEnd(pipe))
    stream = await server.open(tunnel.Open(kind=tunnel.Kind.HTTP, target="/big"))
    for _ in range(1000):
        await stream.offer(tunnel.Op.DATA, b"x")
    with pytest.raises(TunnelDown, match="behind"):
        await _drain_frames(stream)
    sent = []
    while not pipe.down.empty():
        sent.append(tunnel.parse(pipe.down.get_nowait()))
    assert [op for op, ident, _ in sent if ident == stream.id] == [
        tunnel.Op.OPEN,
        tunnel.Op.CLOSE,
    ]


async def test_a_session_without_a_runner_has_no_desktop():
    from uuid import uuid4

    tunnels = Tunnels()
    session_id = uuid4()
    assert not tunnels.connected(session_id)
    with pytest.raises(TunnelDown):
        tunnels.get(session_id)
