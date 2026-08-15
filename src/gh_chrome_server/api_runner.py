import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, WebSocket, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from gh_chrome_protocol import (
    CloseReason,
    CommandEnvelope,
    CommandError,
    CommandResult,
    Download,
    RunnerConfig,
    RunnerEvent,
)
from gh_chrome_server import storage
from gh_chrome_server.auth import SocketToken, Token
from gh_chrome_server.config import settings
from gh_chrome_server.deps import Db, Ss, Tn
from gh_chrome_server.sessions import SessionUnavailable
from gh_chrome_server.sse import Frame, sse_response

router = APIRouter(prefix="/runner", tags=["runner"])

POLL_INTERVAL = 0.2


class Cancel(BaseModel):
    command_id: UUID


class Close(BaseModel):
    reason: CloseReason = CloseReason.CLOSED


@router.get("/{session_id}/config")
async def get_config(session_id: UUID, sessions: Ss, _: Token) -> RunnerConfig:
    state = await sessions.get(session_id)
    return RunnerConfig(
        session_id=state.id,
        params=state.params,
        profile=state.profile,
        persist=state.persist,
        has_profile_archive=state.profile is not None
        and storage.profile_path(state.profile).exists(),
        segment_seconds=settings.segment_seconds,
    )


@router.get("/{session_id}/stream")
async def stream_commands(
    session_id: UUID, request: Request, sessions: Ss, _: Token
) -> Response:
    await sessions.require_live(session_id)
    await sessions.mark_ready(session_id)

    async def frames() -> AsyncGenerator[Frame]:
        while not await request.is_disconnected():
            # Cancels first: a command that timed out in the same breath as the
            # close would otherwise keep the runner busy with work the session
            # has already given up on, and the stream it was told through is
            # gone by then.
            for command_id in sessions.take_cancels(session_id):
                yield Frame(name="cancel", data=Cancel(command_id=command_id))
            if session_id in sessions.closing:
                yield Frame(name="close", data=Close())
                return
            row = await sessions.take_next(session_id)
            if row is None:
                # Only the polite close goes through sessions.closing; a session
                # the watchdog gave up on, or one closed before it ever went
                # active, would otherwise leave the runner holding this stream
                # until the job runs out of hours.
                if not await sessions.live(session_id):
                    yield Frame(name="close", data=Close())
                    return
                await asyncio.sleep(POLL_INTERVAL)
                continue
            yield Frame(
                name="command",
                data=CommandEnvelope(
                    command_id=row["id"],
                    seq=row["seq"],
                    args=row["args"],
                    timeout_ms=row["timeout_ms"],
                    traceparent=row["traceparent"],
                    tracestate=row["tracestate"],
                ),
            )

    return sse_response(frames())


@router.websocket("/{session_id}/tunnel")
async def serve_tunnel(
    session_id: UUID, websocket: WebSocket, tunnels: Tn, _: SocketToken
) -> None:
    await websocket.accept()
    await tunnels.serve(session_id, websocket)


@router.post(
    "/{session_id}/commands/{command_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def complete_command(
    session_id: UUID, command_id: UUID, result: CommandResult, sessions: Ss, _: Token
) -> None:
    error = (
        CommandError.model_validate(result.error) if result.error is not None else None
    )
    await sessions.complete(session_id, command_id, result.result, error)


@router.post("/{session_id}/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def heartbeat(session_id: UUID, sessions: Ss, _: Token) -> None:
    if not await sessions.heartbeat(session_id):
        raise SessionUnavailable("session is not live")


@router.post("/{session_id}/events", status_code=status.HTTP_204_NO_CONTENT)
async def publish_event(
    session_id: UUID, event: RunnerEvent, sessions: Ss, _: Token
) -> None:
    await sessions.publish_runner_event(session_id, event.data)


@router.post("/{session_id}/closed", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_close(session_id: UUID, sessions: Ss, _: Token) -> None:
    await sessions.finish(session_id, CloseReason.CLOSED)


@router.put("/{session_id}/init", status_code=status.HTTP_204_NO_CONTENT)
async def put_init_segment(session_id: UUID, request: Request, _: Token) -> None:
    await storage.write_atomic(
        storage.segments_dir(session_id) / "init.m4s",
        request.stream(),
        settings.max_upload,
    )


@router.put("/{session_id}/segments/{number}", status_code=status.HTTP_204_NO_CONTENT)
async def put_segment(
    session_id: UUID, number: int, request: Request, _: Token
) -> None:
    if number < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad segment number")
    await storage.write_atomic(
        storage.segments_dir(session_id) / f"{number}.m4s",
        request.stream(),
        settings.max_upload,
    )


@router.get("/{session_id}/profile")
async def get_profile(session_id: UUID, sessions: Ss, _: Token) -> FileResponse:
    state = await sessions.get(session_id)
    if state.profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session has no profile")
    path = storage.profile_path(state.profile)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no archive yet")
    return FileResponse(path)


@router.put("/{session_id}/profile", status_code=status.HTTP_204_NO_CONTENT)
async def put_profile(
    session_id: UUID, request: Request, sessions: Ss, db: Db, _: Token
) -> None:
    state = await sessions.get(session_id)
    if state.profile is None or not state.persist:
        raise SessionUnavailable("session does not persist a profile")
    size = await storage.write_atomic(
        storage.profile_path(state.profile), request.stream(), settings.max_upload
    )
    async with db.tx() as tx:
        await tx.run(
            "update profiles set size = %s, stale = false, updated_at = now() where name = %s",
            (size, state.profile),
        )


@router.get("/{session_id}/files/{file_id}")
async def get_upload(session_id: UUID, file_id: UUID, db: Db, _: Token) -> FileResponse:
    row = await db.one(
        "select name from files where id = %s and session_id = %s",
        (file_id, session_id),
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown file")
    path = storage.files_dir(session_id) / f"{file_id}_{row['name']}"
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file is gone")
    return FileResponse(path, filename=row["name"])


@router.put("/{session_id}/downloads/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def put_download(
    session_id: UUID, name: str, request: Request, sessions: Ss, db: Db, _: Token
) -> None:
    safe = storage.safe_name(name)
    size = await storage.write_atomic(
        storage.downloads_dir(session_id) / safe, request.stream(), settings.max_upload
    )
    async with db.tx() as tx:
        await tx.run(
            "insert into downloads (session_id, name, size) values (%s, %s, %s) "
            "on conflict (session_id, name) do update set size = excluded.size",
            (session_id, safe, size),
        )
    url = f"{settings.public_url}/sessions/{session_id}/downloads/{safe}"
    await sessions.publish_runner_event(
        session_id, Download(name=safe, size=size, url=url)
    )
