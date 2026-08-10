from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Request

from gh_chrome_server.db import Database
from gh_chrome_server.events import Events
from gh_chrome_server.sessions import Sessions


def _from_state(name: str) -> Callable[[Request], Any]:
    def get(request: Request) -> Any:
        return getattr(request.app.state, name)

    return get


Db = Annotated[Database, Depends(_from_state("db"))]
Ev = Annotated[Events, Depends(_from_state("events"))]
Ss = Annotated[Sessions, Depends(_from_state("sessions"))]
