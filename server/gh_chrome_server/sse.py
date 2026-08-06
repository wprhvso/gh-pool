from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

KEEPALIVE = 15.0
RETRY_MS = 1000


@dataclass(slots=True)
class Frame:
    name: str
    data: BaseModel
    id: int | None = None


def resume_from(request: Request, fallback: int) -> int:
    raw = request.headers.get("last-event-id")
    if raw is None:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def _encode(frame: Frame) -> str:
    lines: list[str] = []
    if frame.id is not None:
        lines.append(f"id: {frame.id}")
    lines.append(f"event: {frame.name}")
    lines.append(f"data: {frame.data.model_dump_json()}")
    return "\n".join(lines) + "\n\n"


async def _pump(source: AsyncGenerator[Frame]) -> AsyncGenerator[str]:
    yield f"retry: {RETRY_MS}\n\n"
    pending: asyncio.Task[Frame] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(source))
            done, _ = await asyncio.wait({pending}, timeout=KEEPALIVE)
            if not done:
                yield ": ping\n\n"
                continue
            task, pending = pending, None
            try:
                frame = task.result()
            except StopAsyncIteration:
                return
            yield _encode(frame)
    finally:
        if pending is not None:
            pending.cancel()
        await source.aclose()


def sse_response(source: AsyncGenerator[Frame]) -> StreamingResponse:
    return StreamingResponse(
        _pump(source),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
