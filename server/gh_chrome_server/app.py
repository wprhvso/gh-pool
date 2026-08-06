from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gh_chrome_server import api_client, api_player, api_runner, storage
from gh_chrome_server.config import settings
from gh_chrome_server.db import Database
from gh_chrome_server.events import Events
from gh_chrome_server.sessions import Sessions
from gh_chrome_server.watchdog import Watchdog


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    storage.ensure_dirs()
    db = Database(settings.database_url)
    await db.open()
    events = Events(db)
    sessions = Sessions(db, events)
    watchdog = Watchdog(db, sessions)
    app.state.db = db
    app.state.events = events
    app.state.sessions = sessions
    await watchdog.start()
    try:
        yield
    finally:
        await watchdog.stop()
        await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="gh-chrome", lifespan=lifespan)
    app.include_router(api_client.router)
    app.include_router(api_client.profiles_router)
    app.include_router(api_runner.router)
    app.include_router(api_player.router)
    return app


app = create_app()
