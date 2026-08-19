import codecs
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(slots=True)
class SseMessage:
    event: str
    data: str
    # What the sender called this frame, which is how a reader that cannot make
    # sense of the data can still say where it got to.
    id: str | None = None


async def parse_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[SseMessage]:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    event = "message"
    data: list[str] = []
    identifier: str | None = None
    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if data:
                    yield SseMessage(event, "\n".join(data), identifier)
                event, data, identifier = "message", [], None
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line.removeprefix("event:").removeprefix(" ")
            elif line.startswith("id:"):
                identifier = line.removeprefix("id:").removeprefix(" ")
            elif line.startswith("data:"):
                data.append(line.removeprefix("data:").removeprefix(" "))
