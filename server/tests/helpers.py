from __future__ import annotations

from uuid import uuid4

from gh_chrome_protocol import Event
from gh_chrome_protocol.events import CommandStarted


def command_started() -> CommandStarted:
    return CommandStarted(command_id=uuid4())


def fake_event(seq: int) -> Event:
    return Event(seq=seq, data=command_started())
