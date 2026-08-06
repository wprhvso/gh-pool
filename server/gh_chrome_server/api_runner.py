from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from gh_chrome_protocol import (
    CloseReason,
    CommandEnvelope,
    CommandError,
    CommandResult,
    RunnerConfig,
    RunnerEvent,
    SessionStatus,
)
from gh_chrome_protocol.events import Download
from pydantic import BaseModel

from gh_chrome_server import storage
from gh_chrome_server.auth import Token
from gh_chrome_server.config import settings
from gh_chrome_server.deps import Db, Ss
from gh_chrome_server.sessions import SessionNotFound
from gh_chrome_server.sse import Frame, sse_response

router = APIRouter(prefix="/runner", tags=["runner"])

POLL_INTERVAL = 0.2


class Cancel(BaseModel):
    command_id: UUID


class Close(BaseModel):
    reason: CloseReason = CloseReason.CLOSED


class Ok(BaseModel):
    ok: bool = True


async def _require_live(sessions: Ss, session_id: UUID) -> None:
    try:
        state = await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    if state.status in {SessionStatus.CLOSED, SessionStatus.DEAD}:
        raise HTTPException(status.HTTP_409_CONFLICT, f"session is {state.status}")


@router.get("/{session_id}/config")
async def get_config(session_id: UUID, sessions: Ss, _: Token) -> RunnerConfig:
    try:
        state = await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    has_archive = state.profile is not None and storage.profile_path(state.profile).exists()
    return RunnerConfig(
        session_id=state.id,
        params=state.params,
        profile=state.profile,
        persist=state.persist,
        has_profile_archive=has_archive,
        segment_seconds=settings.segment_seconds,
    )


@router.get("/{session_id}/stream")
async def stream_commands(session_id: UUID, request: Request, sessions: Ss, _: Token) -> Response:
    await _require_live(sessions, session_id)
    await sessions.mark_ready(session_id)

    async def frames() -> AsyncGenerator[Frame]:
        while not await request.is_disconnected():
            if session_id in sessions.closing:
                yield Frame(name="close", data=Close())
                return
            for command_id in sessions.take_cancels(session_id):
                yield Frame(name="cancel", data=Cancel(command_id=command_id))
            row = await sessions.take_next(session_id)
            if row is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            yield Frame(
                name="command",
                data=CommandEnvelope(
                    command_id=row["id"],
                    seq=row["seq"],
                    args=row["args"],
                    timeout_ms=row["timeout_ms"],
                ),
            )

    return sse_response(frames())


@router.post("/{session_id}/commands/{command_id}", status_code=status.HTTP_204_NO_CONTENT)
async def complete_command(
    session_id: UUID, command_id: UUID, result: CommandResult, sessions: Ss, _: Token
) -> None:
    error = CommandError.model_validate(result.error) if result.error is not None else None
    await sessions.complete(session_id, command_id, result.result, error)


@router.post("/{session_id}/heartbeat")
async def heartbeat(session_id: UUID, sessions: Ss, _: Token) -> Ok:
    alive = await sessions.heartbeat(session_id)
    if not alive:
        raise HTTPException(status.HTTP_409_CONFLICT, "session is not live")
    return Ok()


@router.post("/{session_id}/events", status_code=status.HTTP_204_NO_CONTENT)
async def publish_event(session_id: UUID, event: RunnerEvent, sessions: Ss, _: Token) -> None:
    await sessions.publish_runner_event(session_id, event.data)


@router.post("/{session_id}/closed", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_close(session_id: UUID, sessions: Ss, _: Token) -> None:
    await sessions.finish(session_id, CloseReason.CLOSED)


@router.put("/{session_id}/init", status_code=status.HTTP_204_NO_CONTENT)
async def put_init_segment(session_id: UUID, request: Request, _: Token) -> None:
    await storage.write_atomic(storage.segments_dir(session_id) / "init.m4s", request.stream())


@router.put("/{session_id}/segments/{number}", status_code=status.HTTP_204_NO_CONTENT)
async def put_segment(session_id: UUID, number: int, request: Request, _: Token) -> None:
    if number < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad segment number")
    await storage.write_atomic(storage.segments_dir(session_id) / f"{number}.m4s", request.stream())


@router.get("/{session_id}/profile")
async def get_profile(session_id: UUID, sessions: Ss, _: Token) -> FileResponse:
    try:
        state = await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    if state.profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session has no profile")
    path = storage.profile_path(state.profile)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no archive yet")
    return FileResponse(path)


@router.put("/{session_id}/profile", status_code=status.HTTP_204_NO_CONTENT)
async def put_profile(session_id: UUID, request: Request, sessions: Ss, db: Db, _: Token) -> None:
    try:
        state = await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    if state.profile is None or not state.persist:
        raise HTTPException(status.HTTP_409_CONFLICT, "session does not persist a profile")
    size = await storage.write_atomic(storage.profile_path(state.profile), request.stream())
    async with db.tx() as tx:
        await tx.conn.execute(
            "update profiles set size = %s, stale = false, updated_at = now() where name = %s",
            (size, state.profile),
        )


@router.get("/{session_id}/files/{file_id}")
async def get_upload(session_id: UUID, file_id: UUID, db: Db, _: Token) -> FileResponse:
    async with db.conn() as conn:
        cur = await conn.execute(
            "select name from files where id = %s and session_id = %s", (file_id, session_id)
        )
        row = await cur.fetchone()
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
    try:
        safe = storage.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad name") from exc
    size = await storage.write_atomic(storage.downloads_dir(session_id) / safe, request.stream())
    async with db.tx() as tx:
        await tx.conn.execute(
            "insert into downloads (session_id, name, size) values (%s, %s, %s) "
            "on conflict (session_id, name) do update set size = excluded.size",
            (session_id, safe, size),
        )
    url = f"{settings.public_url}/sessions/{session_id}/downloads/{safe}"
    await sessions.publish_runner_event(session_id, Download(name=safe, size=size, url=url))
