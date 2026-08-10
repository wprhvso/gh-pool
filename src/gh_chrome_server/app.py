from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response

from gh_chrome_server import api_client, api_player, api_runner, github, storage
from gh_chrome_server.config import settings
from gh_chrome_server.db import Database
from gh_chrome_server.events import Events
from gh_chrome_server.sessions import (
    SessionNotFound,
    Sessions,
    SessionUnavailable,
    TooManySessions,
)
from gh_chrome_server.watchdog import Watchdog

STATUS_CODES: dict[type[Exception], int] = {
    SessionNotFound: status.HTTP_404_NOT_FOUND,
    SessionUnavailable: status.HTTP_409_CONFLICT,
    TooManySessions: status.HTTP_429_TOO_MANY_REQUESTS,
    storage.BadName: status.HTTP_400_BAD_REQUEST,
    github.DispatchError: status.HTTP_502_BAD_GATEWAY,
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    storage.ensure_dirs()
    db = Database(settings.database_url)
    await db.open()
    app.state.db = db
    app.state.events = Events(db)
    app.state.sessions = Sessions(db, app.state.events)
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


def create_app() -> FastAPI:
    app = FastAPI(title="gh-chrome", lifespan=lifespan)
    for error, code in STATUS_CODES.items():
        app.add_exception_handler(error, _reply(code))
    app.include_router(api_client.router)
    app.include_router(api_client.profiles_router)
    app.include_router(api_runner.router)
    app.include_router(api_player.router)
    return app


app = create_app()
