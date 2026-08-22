import logging
from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import Response
from pydantic import BaseModel

from gh_pool.server.deps import Db

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok", "down"]


@router.get("/healthz")
async def healthz(db: Db, response: Response) -> Health:
    try:
        await db.probe()
    except Exception:
        log.warning("the database did not answer", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(status="down")
    return Health(status="ok")
