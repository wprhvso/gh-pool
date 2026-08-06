from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from gh_chrome_server.db import Database
from gh_chrome_server.events import Events
from gh_chrome_server.sessions import Sessions


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_events(request: Request) -> Events:
    events: Events = request.app.state.events
    return events


def get_sessions(request: Request) -> Sessions:
    sessions: Sessions = request.app.state.sessions
    return sessions


Db = Annotated[Database, Depends(get_db)]
Ev = Annotated[Events, Depends(get_events)]
Ss = Annotated[Sessions, Depends(get_sessions)]
