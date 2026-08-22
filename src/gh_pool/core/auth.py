import hashlib
import hmac
import secrets
from typing import Annotated
from uuid import UUID

from fastapi import (
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketException,
    status,
)
from fastapi.responses import Response
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from starlette.requests import HTTPConnection

from gh_pool.core.config import settings
from gh_pool.core.sessions import Sessions

REALM = "gh-chrome"
BASIC_USER = "admin"
TICKET_COOKIE = "gh_chrome_ticket"

_bearer = HTTPBearer(auto_error=True)
_basic = HTTPBasic(auto_error=True, realm=REALM)


def same_secret(offered: str, expected: str) -> bool:
    if not expected:
        return False
    return secrets.compare_digest(offered.encode(), expected.encode())


async def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> None:
    if not same_secret(credentials.credentials, settings.token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid token")


async def require_basic(
    credentials: Annotated[HTTPBasicCredentials, Depends(_basic)],
) -> None:
    user_ok = secrets.compare_digest(credentials.username.encode(), BASIC_USER.encode())
    token_ok = same_secret(credentials.password, settings.token)
    if not (user_ok and token_ok):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid credentials",
            headers={"WWW-Authenticate": f'Basic realm="{REALM}"'},
        )


def ticket(session_id: UUID) -> str:
    return hmac.new(
        settings.token.encode(), str(session_id).encode(), hashlib.sha256
    ).hexdigest()


def hand_out_ticket(response: Response, request: Request, session_id: UUID) -> None:
    response.set_cookie(
        TICKET_COOKIE,
        ticket(session_id),
        path=f"/s/{session_id}",
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https"
        or settings.public_url.startswith("https://"),
    )


async def require_socket_ticket(websocket: WebSocket, session_id: UUID) -> None:
    expected = ticket(session_id)
    offered = (
        websocket.cookies.get(TICKET_COOKIE)
        or websocket.query_params.get("ticket")
        or ""
    )
    if not same_secret(offered, expected):
        raise WebSocketException(status.WS_1008_POLICY_VIOLATION, "invalid ticket")


async def _expected_runner_token(connection: HTTPConnection, session_id: UUID) -> str:
    sessions: Sessions = connection.app.state.sessions
    return await sessions.runner_token(session_id) or ""


async def require_runner(
    request: Request,
    session_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> None:
    expected = await _expected_runner_token(request, session_id)
    if not same_secret(credentials.credentials, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid runner token")


async def require_socket_runner(websocket: WebSocket, session_id: UUID) -> None:
    scheme, _, value = websocket.headers.get("authorization", "").partition(" ")
    expected = await _expected_runner_token(websocket, session_id)
    if scheme.lower() != "bearer" or not same_secret(value, expected):
        raise WebSocketException(status.WS_1008_POLICY_VIOLATION, "invalid token")


Token = Annotated[None, Depends(require_token)]
Runner = Annotated[None, Depends(require_runner)]
Basic = Annotated[None, Depends(require_basic)]
SocketRunner = Annotated[None, Depends(require_socket_runner)]
SocketTicket = Annotated[None, Depends(require_socket_ticket)]
