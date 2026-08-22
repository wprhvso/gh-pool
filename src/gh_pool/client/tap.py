import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from importlib.resources import files
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from gh_pool.client.errors import GhChromeError, TapError, TapRejected, TapTimeout
from gh_pool.client.session import Session

SCRIPT: Final = files("gh_pool.client").joinpath("tap.js").read_text(encoding="utf-8")

DEFAULT_WINDOW: Final = 20.0
COMMAND_MARGIN: Final = 15.0

type Action = Literal["fulfill", "rewrite", "capture"]


class Rule(BaseModel):
    name: str
    url: str
    action: Action
    method: str | None = None
    status: int = 200
    body: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class Captured(BaseModel):
    name: str
    url: str
    method: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


class Frame(BaseModel):
    text: str = ""
    status: int = 0
    error: str | None = None
    done: bool = False


def _call(name: str, *args: object) -> str:
    arguments = ", ".join(json.dumps(argument) for argument in args)
    return f"window.__ghTap.{name}({arguments})"


def _now() -> float:
    return asyncio.get_running_loop().time()


class Tap:
    def __init__(self, session: Session, *, window: float = DEFAULT_WINDOW) -> None:
        self._session = session
        self._window = window
        self._armed = False

    async def arm(self) -> None:
        if self._armed:
            return
        await self._session.init_script(SCRIPT)
        self._armed = True

    async def install(self, rules: Sequence[Rule]) -> None:
        payload = [rule.model_dump(mode="json") for rule in rules]
        await self._session.init_script(f"{SCRIPT};\n{_call('configure', payload)};")
        await self._session.evaluate(SCRIPT)
        await self._session.evaluate(_call("configure", payload))
        self._armed = True

    async def take(self, name: str, *, timeout: float) -> Captured:
        deadline = _now() + timeout
        while True:
            wait = min(self._window, deadline - _now())
            if wait <= 0:
                raise TapTimeout(f"{name} was not requested in {timeout}s")
            found = await self._poll(name, wait)
            if found is not None:
                return Captured.model_validate(found)

    async def replay(
        self, request: Captured, *, body: str | None = None, timeout: float
    ) -> AsyncIterator[str]:
        payload = {
            "url": request.url,
            "method": request.method,
            "headers": request.headers,
            "body": request.body if body is None else body,
        }
        stream_id = str(await self._session.evaluate(_call("replay", payload)))
        try:
            async for text in self._pump(stream_id, timeout):
                yield text
        finally:
            with suppress(GhChromeError):
                await self._session.evaluate(_call("stop", stream_id))

    async def _poll(self, name: str, wait: float) -> Any:
        return await self._session.evaluate(
            _call("take", name, int(wait * 1000)), wait + COMMAND_MARGIN
        )

    async def _pump(self, stream_id: str, timeout: float) -> AsyncIterator[str]:
        deadline = _now() + timeout
        while True:
            wait = min(self._window, deadline - _now())
            if wait <= 0:
                raise TapTimeout(f"the replayed request stalled for {timeout}s")
            frame = Frame.model_validate(
                await self._session.evaluate(
                    _call("read", stream_id, int(wait * 1000)), wait + COMMAND_MARGIN
                )
            )
            if frame.error is not None:
                raise TapError(frame.error)
            if frame.status >= 400:
                raise TapRejected(frame.status, frame.text)
            if frame.text:
                yield frame.text
            if frame.done:
                return
