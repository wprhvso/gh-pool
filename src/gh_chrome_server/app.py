from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from gh_chrome_server import (
    api_client,
    api_player,
    api_runner,
    api_vnc,
    github,
    storage,
)
from gh_chrome_server.config import settings
from gh_chrome_server.db import Database
from gh_chrome_server.events import Events
from gh_chrome_server.sessions import (
    SessionNotFound,
    Sessions,
    SessionUnavailable,
    TooManySessions,
)
from gh_chrome_server.tunnel import TunnelDown, Tunnels
from gh_chrome_server.watchdog import Watchdog

STATUS_CODES: dict[type[Exception], int] = {
    SessionNotFound: status.HTTP_404_NOT_FOUND,
    SessionUnavailable: status.HTTP_409_CONFLICT,
    TooManySessions: status.HTTP_429_TOO_MANY_REQUESTS,
    storage.BadName: status.HTTP_400_BAD_REQUEST,
    storage.TooLarge: status.HTTP_413_CONTENT_TOO_LARGE,
    github.DispatchError: status.HTTP_502_BAD_GATEWAY,
    TunnelDown: status.HTTP_503_SERVICE_UNAVAILABLE,
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    storage.ensure_dirs()
    db = Database(settings.database_url)
    await db.open()
    app.state.db = db
    app.state.events = Events(db)
    app.state.sessions = Sessions(db, app.state.events)
    app.state.tunnels = Tunnels()
    watchdog = Watchdog(app.state.sessions)
    await watchdog.start()
    try:
        yield
    finally:
        await watchdog.stop()
        await db.close()


def _reply(code: int) -> Callable[[Request, Exception], Response]:
    def handler(_: Request, exc: Exception) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=code)

    return handler


def install_errors(app: FastAPI) -> None:
    for error, code in STATUS_CODES.items():
        app.add_exception_handler(error, _reply(code))


class LimitBody:
    """Turns away a body bigger than the server will keep, before anything reads it.

    The multipart parser spools a whole upload to a temporary file before the
    handler it belongs to is ever called, on a volume the operator sized for
    something else, so a limit checked inside the handler is checked far too
    late. A body with no length declared is left to the handler, which counts
    as it writes.
    """

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self._app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            declared = Headers(scope=scope).get("content-length", "")
            if declared.isdigit() and int(declared) > self._limit:
                response = JSONResponse(
                    {"detail": f"more than {self._limit} bytes"},
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="gh-chrome", lifespan=lifespan)
    app.add_middleware(LimitBody, limit=settings.max_upload)
    install_errors(app)
    app.include_router(api_client.router)
    app.include_router(api_client.profiles_router)
    app.include_router(api_runner.router)
    app.include_router(api_vnc.router)
    app.include_router(api_player.router)
    return app


app = create_app()
