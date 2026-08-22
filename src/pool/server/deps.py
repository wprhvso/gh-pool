from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends
from starlette.requests import HTTPConnection

from pool.server.db import Database
from pool.server.events import Events
from pool.server.sessions import Sessions
from pool.relay.tunnel import Tunnels


def _from_state(name: str) -> Callable[[HTTPConnection], Any]:
    def get(connection: HTTPConnection) -> Any:
        return getattr(connection.app.state, name)

    return get


Db = Annotated[Database, Depends(_from_state("db"))]
Ev = Annotated[Events, Depends(_from_state("events"))]
Ss = Annotated[Sessions, Depends(_from_state("sessions"))]
Tn = Annotated[Tunnels, Depends(_from_state("tunnels"))]
