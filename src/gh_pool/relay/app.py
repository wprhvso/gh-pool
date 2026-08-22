import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, FastAPI, status
from fastapi.responses import Response
from pydantic import BaseModel

from gh_pool.core import errors
from gh_pool.core.config import settings
from gh_pool.core.db import Database
from gh_pool.core.deps import Db
from gh_pool.core.errors import Codes
from gh_pool.core.events import Events
from gh_pool.core.sessions import Sessions
from gh_pool.relay import api_runner, vnc
from gh_pool.relay.deps import Tn
from gh_pool.relay.tunnel import TunnelDown, Tunnels

log = logging.getLogger(__name__)

STATUS_CODES: Codes = {
    TunnelDown: status.HTTP_503_SERVICE_UNAVAILABLE,
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
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
    errors.install(app, STATUS_CODES)
    app.include_router(health)
    app.include_router(api_runner.router)
    app.include_router(vnc.router)
    return app


app = create_app()
