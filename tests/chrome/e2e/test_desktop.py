import asyncio
import contextlib
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import pytest
import websockets
from tests.chrome.e2e.stack import Stack
from tests.test_tunnel import BODY, _desktop
from websockets.exceptions import InvalidStatus
from websockets.typing import Subprotocol

from gh_pool.client import Session
from gh_pool.browser.tunnel import Tunnel
from gh_pool.server import auth


async def _wait_connected(
    player: httpx.AsyncClient, session_id: UUID, timeout: float = 30.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        status = await player.get(f"/s/{session_id}/vnc.json")
        if status.json()["connected"]:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("the runner's tunnel never reached the server")
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def _connected(
    player: httpx.AsyncClient, session: Session
) -> AsyncGenerator[None]:
    async with _desktop() as port:
        tunnel = Tunnel(session.id, port)
        await tunnel.start()
        try:
            await _wait_connected(player, session.id)
            yield
        finally:
            await tunnel.stop()


def _socket_url(base: str, path: str) -> str:
    return f"ws://{urlsplit(base).netloc}{path}"


async def test_a_session_without_a_runner_has_no_desktop(
    stack: Stack, player: httpx.AsyncClient
):
    session, _ = await stack.scripted()

    status = await player.get(f"/s/{session.id}/vnc.json")

    assert status.status_code == 200
    assert status.json() == {"connected": False, "ticket": auth.ticket(session.id)}
    assert (await player.get(f"/s/{session.id}/vnc/index.html")).status_code == 503


async def test_a_desktop_file_is_served_through_the_server(
    stack: Stack, player: httpx.AsyncClient
):
    session, _ = await stack.scripted()

    async with _connected(player, session):
        response = await player.get(f"/s/{session.id}/vnc/asset.js")

    assert response.status_code == 200
    assert response.content == BODY
    assert response.cookies[auth.TICKET_COOKIE] == auth.ticket(session.id)


async def test_the_desktop_is_closed_to_a_stranger(
    stack: Stack, player: httpx.AsyncClient
):
    session, _ = await stack.scripted()

    async with (
        _connected(player, session),
        httpx.AsyncClient(base_url=stack.server.url, timeout=30.0) as stranger,
    ):
        assert (await stranger.get(f"/s/{session.id}/vnc.json")).status_code == 401
        assert (await stranger.get(f"/s/{session.id}/vnc/asset.js")).status_code == 401


async def test_the_desktop_socket_relays_frames_both_ways(
    stack: Stack, player: httpx.AsyncClient
):
    session, _ = await stack.scripted()
    ticket = auth.ticket(session.id)

    async with _connected(player, session):
        url = _socket_url(
            stack.server.url, f"/s/{session.id}/vnc/socket?ticket={ticket}"
        )
        async with websockets.connect(
            url, subprotocols=[Subprotocol("binary")]
        ) as socket:
            await socket.send(b"RFB 003.008")
            assert await socket.recv() == b"RFB 003.008"
            await socket.send("a text frame")
            assert await socket.recv() == "a text frame"


async def test_the_desktop_socket_refuses_a_forged_ticket(
    stack: Stack, player: httpx.AsyncClient
):
    session, _ = await stack.scripted()

    async with _connected(player, session):
        url = _socket_url(stack.server.url, f"/s/{session.id}/vnc/socket?ticket=nope")
        with pytest.raises(InvalidStatus):
            async with websockets.connect(url):
                pass
