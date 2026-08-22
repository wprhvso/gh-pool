from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gh_pool.protocol import trace
from gh_pool.relay import vnc as api_vnc
from gh_pool.relay.tunnel import TunnelDown, Tunnels
from gh_pool.server import (
    api_client,
    api_health,
    api_player,
    api_runner,
    pool,
    storage,
)
from gh_pool.server.cleaner import Cleaner
from gh_pool.server.config import settings
from gh_pool.server.db import Database
from gh_pool.server.events import Events
from gh_pool.server.sessions import (
    SessionNotFound,
    Sessions,
    SessionUnavailable,
    TooManySessions,
)
from gh_pool.server.watchdog import Watchdog

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
    cleaner = Cleaner(app.state.sessions)
    await cleaner.start()
    try:
        yield
    finally:
        await cleaner.stop()
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
    def __init__(self, app: ASGIApp, limit: int) -> None:
        self._app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self._limit:
            await self._refuse(scope, receive, send)
            return
        seen = 0
        over = False
        answered = False

        async def counting() -> Message:
            nonlocal seen, over
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._limit:
                    over = True
                    raise storage.TooLarge(f"more than {self._limit} bytes")
            return message

        async def sending(message: Message) -> None:
            nonlocal answered
            if not over:
                await send(message)
                return
            if not answered and message["type"] == "http.response.start":
                answered = True
                await self._refuse(scope, counting, send)

        await self._app(scope, counting, sending)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": f"more than {self._limit} bytes"},
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )
        await response(scope, receive, send)


class BindTrace:
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
    app.add_middleware(BindTrace)
    install_errors(app)
    app.include_router(api_health.router)
    app.include_router(api_client.router)
    app.include_router(api_client.profiles_router)
    app.include_router(api_runner.router)
    app.include_router(api_vnc.router)
    app.include_router(api_player.router)
    return app


app = create_app()
