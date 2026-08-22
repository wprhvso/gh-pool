from __future__ import annotations

import io
import urllib.error
from email.message import Message
from typing import Self

import pytest
from pool_runners import http as http_mod


class Clock:
    def __init__(self, start: float = 1000.0, wall: float = 1_700_000_000.0) -> None:
        self.now = start
        self.wall = wall

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall

    def tick(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds


def headers(**pairs: str) -> Message:
    message = Message()
    for name, value in pairs.items():
        message[name.replace("_", "-")] = value
    return message


class Response:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"{}",
        url: str = "https://api.github.com/x",
        **head: str,
    ) -> None:
        self.status = status
        self.url = url
        self.headers = headers(**head)
        self._body = body

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def http_error(
    status: int,
    body: bytes = b"{}",
    url: str = "https://api.github.com/x",
    **head: str,
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, "нет", headers(**head), io.BytesIO(body))


@pytest.fixture(autouse=True)
def clear_stop():
    http_mod.STOP.clear()
    yield
    http_mod.STOP.clear()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    from pool_runners import budget as budget_mod

    made = Clock()
    monkeypatch.setattr(budget_mod, "time", made)
    return made
