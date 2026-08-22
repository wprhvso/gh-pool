from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends
from starlette.requests import HTTPConnection

from gh_pool.core.db import Database
from gh_pool.core.events import Events
from gh_pool.core.sessions import Sessions


def from_state(name: str) -> Callable[[HTTPConnection], Any]:
    def get(connection: HTTPConnection) -> Any:
        return getattr(connection.app.state, name)

    return get


Db = Annotated[Database, Depends(from_state("db"))]
Ev = Annotated[Events, Depends(from_state("events"))]
Ss = Annotated[Sessions, Depends(from_state("sessions"))]
