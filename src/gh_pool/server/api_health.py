import logging
from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import Response
from pydantic import BaseModel

from gh_pool.core.deps import Db
from gh_pool.server import tasks

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok", "down"]
    ok: bool
    tasks: dict[str, int]
    queue: int
    workers: int
    started_at: float
    uptime: float
    pending_writes: int
    db: bool


@router.get("/healthz")
async def healthz(db: Db, response: Response) -> Health:
    health = Health(**tasks.report(), status="ok")
    try:
        await db.probe()
    except Exception:
        log.warning("the database did not answer", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        health.status = "down"
        health.ok = False
        health.db = False
    return health
