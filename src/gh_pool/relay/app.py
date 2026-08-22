import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gh_pool.relay import vnc
from gh_pool.relay.tunnel import Tunnels
from gh_pool.server.config import settings
from gh_pool.server.db import Database
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


def create_app() -> FastAPI:
    app = FastAPI(title="gh-pool-relay", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Готовность не зависит от наличия туннелей.

        Пустой relay полностью работоспособен — он просто ещё никому не
        понадобился. Число туннелей отдаётся рядом, оно нужно для драйна:
        по нему видно, можно ли гасить процесс, не оборвав живую сессию.
        """
        try:
            await app.state.db.probe()
        except Exception:
            log.exception("healthz: база не отвечает")
            return JSONResponse({"status": "down"}, status_code=503)
        return JSONResponse({"status": "ok", "tunnels": app.state.tunnels.count()})

    app.include_router(vnc.router)
    return app


app = create_app()
