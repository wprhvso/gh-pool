import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, FastAPI, status
from fastapi.responses import Response
from pydantic import BaseModel

from gh_pool.relay import api_runner, vnc
from gh_pool.relay.tunnel import Tunnels
from gh_pool.server.config import settings
from gh_pool.server.db import Database
from gh_pool.server.deps import Db, Tn
from gh_pool.server.events import Events
from gh_pool.server.sessions import Sessions

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Relay открывает базу, но не мигрирует её и не пасёт фоновые задачи.

    Миграции и сторож принадлежат server: гонять их из двух процессов —
    верный способ получить состязание на старте. Сюда база нужна только для
    авторизации раннера и выдачи очередной команды.
    """
    db = Database(settings.database_url)
    await db.open(migrate=False)
    app.state.db = db
    app.state.events = Events(db)
    app.state.sessions = Sessions(db, app.state.events)
    app.state.tunnels = Tunnels()
    try:
        yield
    finally:
        await db.close()


class Health(BaseModel):
    status: Literal["ok", "down"]
    tunnels: int


health = APIRouter(tags=["health"])


@health.get("/healthz")
async def healthz(db: Db, tunnels: Tn, response: Response) -> Health:
    try:
        await db.probe()
    except Exception:
        log.warning("the database did not answer", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(status="down", tunnels=tunnels.count())
    return Health(status="ok", tunnels=tunnels.count())


def create_app() -> FastAPI:
    app = FastAPI(title="gh-pool-relay", lifespan=lifespan)
    app.include_router(health)
    app.include_router(api_runner.router)
    app.include_router(vnc.router)
    return app


app = create_app()
