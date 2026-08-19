from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gh_chrome_server import auth
from gh_chrome_server.config import settings

TOKEN = "a-shared-secret"
SESSION = UUID("4b1f6bd6-2c4a-4e2f-9a51-6f7a2c0d0f11")
RUNNER_TOKEN = "the-token-this-session-was-given"


class FakeSessions:
    def __init__(self, token: str | None) -> None:
        self._token = token

    async def runner_token(self, _session_id: UUID) -> str | None:
        return self._token


def _app(runner_token: str | None = RUNNER_TOKEN) -> FastAPI:
    app = FastAPI()
    app.state.sessions = FakeSessions(runner_token)

    async def shared(_: auth.Token) -> dict[str, bool]:
        return {"ok": True}

    async def player(_: auth.Basic) -> dict[str, bool]:
        return {"ok": True}

    async def runner(session_id: UUID, _: auth.Runner) -> dict[str, bool]:
        return {"ok": True}

    async def socket(websocket: WebSocket, _: auth.SocketToken) -> None:
        await websocket.accept()
        await websocket.close()

    async def desktop(
        websocket: WebSocket, session_id: UUID, _: auth.SocketTicket
    ) -> None:
        await websocket.accept()
        await websocket.close()

    app.add_api_route("/shared", shared, methods=["GET"])
    app.add_api_route("/player", player, methods=["GET"])
    app.add_api_route("/runner/{session_id}", runner, methods=["GET"])
    app.add_api_websocket_route("/socket", socket)
    app.add_api_websocket_route("/desktop/{session_id}", desktop)
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "token", TOKEN)
    with TestClient(_app()) as started:
        yield started


def test_the_shared_token_opens_the_api(client: TestClient):
    answer = client.get("/shared", headers={"Authorization": f"Bearer {TOKEN}"})

    assert answer.status_code == 200


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer ", "Bearer not-the-secret", f"Basic {TOKEN}", TOKEN],
)
def test_anything_that_is_not_the_token_is_turned_away(
    client: TestClient, header: str | None
):
    headers = {} if header is None else {"Authorization": header}

    answer = client.get("/shared", headers=headers)

    assert answer.status_code in {401, 403}


def test_a_credential_that_is_not_ascii_is_refused_rather_than_a_server_error(
    client: TestClient,
):
    answer = client.get(
        "/shared", headers={"Authorization": "Bearer café".encode("latin-1")}
    )

    assert answer.status_code == 403


def test_a_player_needs_the_token_as_its_password(client: TestClient):
    assert client.get("/player", auth=(auth.BASIC_USER, TOKEN)).status_code == 200
    assert client.get("/player", auth=(auth.BASIC_USER, "nope")).status_code == 401
    assert client.get("/player", auth=("someone", TOKEN)).status_code == 401
    assert client.get("/player").status_code == 401


def test_a_refused_player_is_told_how_to_authenticate(client: TestClient):
    answer = client.get("/player")

    assert answer.headers["www-authenticate"].startswith("Basic")
    assert auth.REALM in answer.headers["www-authenticate"]


def test_a_server_without_a_token_configured_lets_nobody_in(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "token", "")

    with TestClient(_app()) as started:
        assert started.get(
            "/shared", headers={"Authorization": "Bearer "}
        ).status_code in {
            401,
            403,
        }
        assert started.get("/player", auth=(auth.BASIC_USER, "")).status_code == 401


def test_a_runner_is_known_by_the_token_its_own_session_was_given(client: TestClient):
    allowed = client.get(
        f"/runner/{SESSION}", headers={"Authorization": f"Bearer {RUNNER_TOKEN}"}
    )
    refused = client.get(
        f"/runner/{SESSION}", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert allowed.status_code == 200
    assert refused.status_code == 403


def test_a_session_with_no_token_left_admits_no_runner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "token", TOKEN)

    with TestClient(_app(runner_token=None)) as started:
        answer = started.get(
            f"/runner/{SESSION}", headers={"Authorization": "Bearer anything"}
        )

    assert answer.status_code == 403


def test_a_ticket_belongs_to_one_session_and_one_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "token", TOKEN)
    mine = auth.ticket(SESSION)

    assert mine == auth.ticket(SESSION)
    assert mine != auth.ticket(uuid4())

    monkeypatch.setattr(settings, "token", "a-different-secret")
    assert mine != auth.ticket(SESSION)


def test_the_socket_takes_the_shared_token_as_a_bearer(client: TestClient):
    with client.websocket_connect(
        "/socket", headers={"Authorization": f"Bearer {TOKEN}"}
    ):
        pass

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/socket", headers={"Authorization": f"Bearer {TOKEN}x"}
        ):
            pass


def test_the_desktop_socket_takes_the_ticket_from_the_query_or_a_cookie(
    client: TestClient,
):
    granted = auth.ticket(SESSION)

    with client.websocket_connect(f"/desktop/{SESSION}?ticket={granted}"):
        pass

    client.cookies.set(auth.TICKET_COOKIE, granted)
    with client.websocket_connect(f"/desktop/{SESSION}"):
        pass


def test_the_desktop_socket_refuses_a_ticket_minted_for_another_session(
    client: TestClient,
):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/desktop/{SESSION}?ticket={auth.ticket(uuid4())}"
        ):
            pass


def test_a_ticket_that_is_not_ascii_is_refused_rather_than_a_server_error(
    client: TestClient,
):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/desktop/{SESSION}?ticket=билет"):
            pass
