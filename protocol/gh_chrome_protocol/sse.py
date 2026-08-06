from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(slots=True)
class SseMessage:
    event: str
    data: str
    id: str | None


async def parse_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[SseMessage]:
    buffer = ""
    last_id: str | None = None
    name = "message"
    payload: list[str] = []
    async for chunk in chunks:
        buffer += chunk.decode("utf-8", "replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if payload:
                    yield SseMessage(event=name, data="\n".join(payload), id=last_id)
                name = "message"
                payload = []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                name = value
            elif field == "data":
                payload.append(value)
            elif field == "id" and "\x00" not in value:
                last_id = value
