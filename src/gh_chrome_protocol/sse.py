"""Just enough of the server-sent events format to read a stream."""

from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(slots=True)
class SseMessage:
    event: str
    data: str


async def parse_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[SseMessage]:
    buffer = ""
    event = "message"
    data: list[str] = []
    async for chunk in chunks:
        buffer += chunk.decode("utf-8", "replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:  # a blank line dispatches the message
                if data:
                    yield SseMessage(event, "\n".join(data))
                event, data = "message", []
            elif line.startswith(":"):
                continue  # comment, used as a keepalive
            elif line.startswith("event:"):
                event = line.removeprefix("event:").removeprefix(" ")
            elif line.startswith("data:"):
                data.append(line.removeprefix("data:").removeprefix(" "))
