import asyncio
import contextlib
from collections.abc import Iterable
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, WebSocket, status
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from gh_pool.protocol import tunnel
from gh_pool.server.auth import Basic, SocketTicket, hand_out_ticket, ticket
from gh_pool.server.deps import Tn
from gh_pool.relay.tunnel import Stream, TunnelDown

router = APIRouter(prefix="/s", tags=["vnc"])

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
MAX_BODY = 32 << 20

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
DROP_UP = HOP_BY_HOP | {"host", "authorization", "cookie", "content-length"}
DROP_DOWN = HOP_BY_HOP | {"content-length", "www-authenticate", "set-cookie"}


class Desktop(BaseModel):
    connected: bool
    ticket: str


@router.get("/{session_id}/vnc.json")
async def desktop_status(session_id: UUID, tunnels: Tn, _: Basic) -> Desktop:
    return Desktop(connected=tunnels.connected(session_id), ticket=ticket(session_id))


@router.get("/{session_id}/vnc")
async def desktop_root(session_id: UUID, _: Basic) -> RedirectResponse:
    return RedirectResponse(f"/s/{session_id}/vnc/")


@router.api_route("/{session_id}/vnc/{path:path}", methods=METHODS)
async def desktop_http(
    session_id: UUID, path: str, request: Request, tunnels: Tn, _: Basic
) -> Response:
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "body is too large")
    stream = await tunnels.get(session_id).open(
        tunnel.Open(
            kind=tunnel.Kind.HTTP,
            target=_target(path, request.url.query),
            method=request.method,
            headers=_headers(request.headers.items(), DROP_UP),
        )
    )
    try:
        body = await request.body()
        for offset in range(0, len(body), tunnel.CHUNK):
            await stream.send(tunnel.Op.DATA, body[offset : offset + tunnel.CHUNK])
        await stream.send(tunnel.Op.EOF)
        head = await stream.head()
    except BaseException:
        await stream.close()
        raise
    response = StreamingResponse(
        stream.body(),
        status_code=head.status,
        headers=_reply_headers(head.headers, f"/s/{session_id}/vnc"),
    )
    hand_out_ticket(response, request, session_id)
    return response


@router.websocket("/{session_id}/vnc/{path:path}")
async def desktop_socket(
    session_id: UUID, path: str, websocket: WebSocket, tunnels: Tn, _: SocketTicket
) -> None:
    stream: Stream | None = None
    offered = _subprotocols(websocket)
    try:
        stream = await tunnels.get(session_id).open(
            tunnel.Open(
                kind=tunnel.Kind.WS,
                target=_target(path, _query(websocket)),
                subprotocols=offered,
            )
        )
        head = await stream.head()
        if head.status != status.HTTP_101_SWITCHING_PROTOCOLS:
            raise TunnelDown(f"the desktop answered {head.status}")
        chosen = head.subprotocol if head.subprotocol in offered else None
        await websocket.accept(subprotocol=chosen)
        await _relay(websocket, stream)
    except (TunnelDown, TimeoutError, WebSocketDisconnect, RuntimeError) as exc:
        with contextlib.suppress(Exception):
            await websocket.close(status.WS_1011_INTERNAL_ERROR, str(exc)[:120])
    finally:
        if stream is not None:
            await stream.close()


async def _relay(websocket: WebSocket, stream: Stream) -> None:
    tasks = {
        asyncio.create_task(_upstream(websocket, stream)),
        asyncio.create_task(_downstream(websocket, stream)),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for task in done:
        task.result()


async def _upstream(websocket: WebSocket, stream: Stream) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        if (data := message.get("bytes")) is not None:
            await _forward(websocket, stream, tunnel.Op.DATA, data)
        elif (text := message.get("text")) is not None:
            await _forward(websocket, stream, tunnel.Op.TEXT, text.encode())


async def _forward(
    websocket: WebSocket, stream: Stream, op: tunnel.Op, payload: bytes
) -> None:
    if len(payload) > tunnel.MAX_PAYLOAD - tunnel.HEADER:
        await websocket.close(code=1009)
        raise WebSocketDisconnect(1009)
    await stream.send(op, payload)


async def _downstream(websocket: WebSocket, stream: Stream) -> None:
    while (chunk := await stream.read()) is not None:
        op, payload = chunk
        if op is tunnel.Op.TEXT:
            await websocket.send_text(payload.decode("utf-8", "replace"))
        else:
            await websocket.send_bytes(payload)


def _target(path: str, query: str) -> str:
    return f"/{path}?{query}" if query else f"/{path}"


def _query(websocket: WebSocket) -> str:
    parts = websocket.url.query.split("&")
    return "&".join(p for p in parts if p and not p.startswith("ticket="))


def _subprotocols(websocket: WebSocket) -> list[str]:
    raw = websocket.headers.get("sec-websocket-protocol", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _headers(
    items: Iterable[tuple[str, str]], drop: frozenset[str]
) -> list[tuple[str, str]]:
    return [(name, value) for name, value in items if name.lower() not in drop]


def _reply_headers(items: Iterable[tuple[str, str]], prefix: str) -> dict[str, str]:
    sent = {}
    for name, value in _headers(items, DROP_DOWN):
        rooted = name.lower() == "location" and value.startswith("/")
        sent[name] = prefix + value if rooted else value
    if not any(name.lower() == "cache-control" for name in sent):
        sent["Cache-Control"] = "no-cache"
    return sent
