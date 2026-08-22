import asyncio
import contextlib
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gh_pool.browser.tunnel import Link
from gh_pool.relay import vnc as api_vnc
from gh_pool.relay.tunnel import Tunnels
from gh_pool.server import auth
from gh_pool.server.app import install_errors
from gh_pool.server.config import settings
from tests.chrome.test_tunnel import Pipe, RunnerEnd, ServerEnd, _desktop

TOKEN = "a-shared-secret"
SESSION = uuid4()
ADMIN = (auth.BASIC_USER, TOKEN)


@contextlib.asynccontextmanager
async def _lifespan(app):
    async with _desktop() as port:
        pipe = Pipe()
        app.state.tunnels = Tunnels()
        link = Link(RunnerEnd(pipe), port)
        tasks = [
            asyncio.create_task(app.state.tunnels.serve(SESSION, ServerEnd(pipe))),
            asyncio.create_task(link.pump()),
        ]
        while not app.state.tunnels.connected(SESSION):
            await asyncio.sleep(0)
        try:
            yield
        finally:
            await link.shutdown()
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "token", TOKEN)
    app = FastAPI(lifespan=_lifespan)
    install_errors(app)
    app.include_router(api_vnc.router)
    with TestClient(app) as started:
        yield started


def test_the_desktop_needs_credentials(client):
    assert client.get(f"/s/{SESSION}/vnc.json").status_code == 401


def test_the_status_reports_a_connected_runner(client):
    response = client.get(f"/s/{SESSION}/vnc.json", auth=ADMIN)
    assert response.status_code == 200
    assert response.json() == {"connected": True, "ticket": auth.ticket(SESSION)}


def test_an_idle_session_reports_no_desktop(client):
    other = uuid4()
    assert client.get(f"/s/{other}/vnc.json", auth=ADMIN).json()["connected"] is False
    assert client.get(f"/s/{other}/vnc/index.html", auth=ADMIN).status_code == 503


def test_the_client_is_proxied_to_the_runner(client):
    response = client.get(f"/s/{SESSION}/vnc/asset.js", auth=ADMIN)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain"
    assert response.content == b"a live desktop" * 500
    assert response.cookies[auth.TICKET_COOKIE] == auth.ticket(SESSION)


def test_a_missing_file_keeps_its_status(client):
    assert client.get(f"/s/{SESSION}/vnc/nope", auth=ADMIN).status_code == 404


def test_the_index_is_reachable_at_the_directory_root(client):
    bare = client.get(f"/s/{SESSION}/vnc", auth=ADMIN, follow_redirects=False)
    assert bare.status_code == 307
    assert bare.headers["location"].endswith(f"/s/{SESSION}/vnc/")
    assert client.get(f"/s/{SESSION}/vnc/", auth=ADMIN).status_code == 200


def test_a_redirect_from_the_desktop_is_moved_under_the_session(client):
    moved = client.get(f"/s/{SESSION}/vnc/moved", auth=ADMIN, follow_redirects=False)

    assert moved.status_code == 302
    assert moved.headers["location"] == f"/s/{SESSION}/vnc/vnc/"


def test_a_body_bigger_than_the_proxy_will_carry_is_refused(client):
    refused = client.post(
        f"/s/{SESSION}/vnc/asset.js",
        auth=ADMIN,
        content=b"x",
        headers={"content-length": str(api_vnc.MAX_BODY + 1)},
    )

    assert refused.status_code == 413


def _connect(client, url):
    with client.websocket_connect(url):
        pass


def test_the_socket_refuses_a_forged_ticket(client):
    with pytest.raises(WebSocketDisconnect):
        _connect(client, f"/s/{SESSION}/vnc/socket?ticket=nope")


def test_the_socket_relays_to_the_desktop(client):
    ticket = auth.ticket(SESSION)
    url = f"/s/{SESSION}/vnc/socket?ticket={ticket}"
    with client.websocket_connect(url) as socket:
        socket.send_bytes(b"\x00rfb")
        assert socket.receive_bytes() == b"\x00rfb"
        socket.send_text("hello")
        assert socket.receive_text() == "hello"


def test_the_binary_subprotocol_is_echoed_back(client):
    url = f"/s/{SESSION}/vnc/socket?ticket={auth.ticket(SESSION)}"
    with client.websocket_connect(url, subprotocols=["binary"]) as socket:
        assert socket.accepted_subprotocol == "binary"
        socket.send_bytes(b"RFB 003.008")
        assert socket.receive_bytes() == b"RFB 003.008"
