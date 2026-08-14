from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field

HEADER = 5
MAX_PAYLOAD = 8 << 20
CHUNK = 1 << 16
BINARY = "binary"


class Op(IntEnum):
    OPEN = 1
    HEAD = 2
    DATA = 3
    TEXT = 4
    EOF = 5
    CLOSE = 6


class Kind(StrEnum):
    HTTP = "http"
    WS = "ws"


class Open(BaseModel):
    kind: Kind
    target: str
    method: str = "GET"
    headers: list[tuple[str, str]] = Field(default_factory=list)
    subprotocols: list[str] = Field(default_factory=list)


class Head(BaseModel):
    status: int
    headers: list[tuple[str, str]] = Field(default_factory=list)
    subprotocol: str | None = None


class Close(BaseModel):
    error: str | None = None


_KNOWN = {int(op) for op in Op}


def frame(op: Op, stream: int, payload: bytes | BaseModel = b"") -> bytes:
    body = payload if isinstance(payload, bytes) else payload.model_dump_json().encode()
    return bytes((op,)) + stream.to_bytes(4, "big") + body


def parse(data: bytes) -> tuple[Op | None, int, bytes]:
    """The op is None for a frame this build has no meaning for.

    One tunnel carries every stream, so an op from a newer other end is skipped
    the way an unknown stream id is, rather than taking the desktop down.
    """
    if len(data) < HEADER:
        raise ValueError("truncated tunnel frame")
    op = Op(data[0]) if data[0] in _KNOWN else None
    return op, int.from_bytes(data[1:HEADER], "big"), data[HEADER:]
