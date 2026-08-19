from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gh_chrome_protocol import trace
from gh_chrome_server import (
    api_client,
    api_player,
    api_runner,
    api_vnc,
    pool,
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
    pool.DispatchError: status.HTTP_502_BAD_GATEWAY,
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
    late. A declared length is refused outright; a body that declares none is
    counted as it is read, because a chunked upload would otherwise be spooled
    in full before anyone could object to its size.
    """

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self._app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self._limit:
            response = JSONResponse(
                {"detail": f"more than {self._limit} bytes"},
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
            await response(scope, receive, send)
            return
        await self._app(scope, self._counted(receive), send)

    def _counted(self, receive: Receive) -> Receive:
        seen = 0

        async def counting() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._limit:
                    raise storage.TooLarge(f"more than {self._limit} bytes")
            return message

        return counting


class BindTrace:
    """Puts the caller's trace on everything the request goes on to log.

    Outermost, so a body turned away by LimitBody below is still reported
    against the trace that sent it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        with trace.bound(trace.TraceContext.from_headers(Headers(scope=scope))):
            await self._app(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="gh-chrome", lifespan=lifespan)
    app.add_middleware(LimitBody, limit=settings.max_upload)
    # Added last, so it wraps the one above rather than sitting inside it.
    app.add_middleware(BindTrace)
    install_errors(app)
    app.include_router(api_client.router)
    app.include_router(api_client.profiles_router)
    app.include_router(api_runner.router)
    app.include_router(api_vnc.router)
    app.include_router(api_player.router)
    return app


app = create_app()
