from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends
from starlette.requests import HTTPConnection

from gh_chrome_server.db import Database
from gh_chrome_server.events import Events
from gh_chrome_server.sessions import Sessions
from gh_chrome_server.tunnel import Tunnels


def _from_state(name: str) -> Callable[[HTTPConnection], Any]:
    def get(connection: HTTPConnection) -> Any:
        return getattr(connection.app.state, name)

    return get


Db = Annotated[Database, Depends(_from_state("db"))]
Ev = Annotated[Events, Depends(_from_state("events"))]
Ss = Annotated[Sessions, Depends(_from_state("sessions"))]
Tn = Annotated[Tunnels, Depends(_from_state("tunnels"))]
