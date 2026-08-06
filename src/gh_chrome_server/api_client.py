from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from gh_chrome_protocol import (
    CloseReason,
    CommandAccepted,
    CommandRequest,
    ProfileInfo,
    SessionCreate,
    SessionState,
    SessionStatus,
)
from pydantic import BaseModel

from gh_chrome_server import github, storage
from gh_chrome_server.auth import Token
from gh_chrome_server.deps import Db, Ev, Ss
from gh_chrome_server.sessions import (
    SessionNotFound,
    SessionUnavailable,
    TooManySessions,
)
from gh_chrome_server.sse import Frame, resume_from, sse_response

router = APIRouter(prefix="/sessions", tags=["client"])
profiles_router = APIRouter(prefix="/profiles", tags=["client"])


class FileAccepted(BaseModel):
    file_id: UUID


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(request: SessionCreate, sessions: Ss, _: Token) -> SessionState:
    try:
        state = await sessions.create(request)
    except TooManySessions as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    try:
        await github.dispatch(state.id)
    except github.DispatchError as exc:
        await sessions.finish(state.id, CloseReason.DEAD)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return state


@router.get("/{session_id}")
async def get_session(session_id: UUID, sessions: Ss, _: Token) -> SessionState:
    try:
        return await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc


@router.post("/{session_id}/commands", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_command(
    session_id: UUID, request: CommandRequest, sessions: Ss, _: Token
) -> CommandAccepted:
    try:
        command_id, seq = await sessions.enqueue(session_id, request)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    except SessionUnavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"session is {exc}") from exc
    return CommandAccepted(command_id=command_id, seq=seq)


@router.get("/{session_id}/events")
async def stream_events(
    session_id: UUID, request: Request, sessions: Ss, events: Ev, _: Token, last_seq: int = 0
) -> Response:
    try:
        await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    after = resume_from(request, last_seq)

    async def frames() -> AsyncGenerator[Frame]:
        async for event in events.stream(session_id, after):
            yield Frame(name=str(event.data.type), data=event, id=event.seq)

    return sse_response(frames())


@router.post("/{session_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(session_id: UUID, sessions: Ss, _: Token) -> None:
    try:
        await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    await sessions.request_close(session_id)


@router.post("/{session_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    session_id: UUID, file: UploadFile, sessions: Ss, db: Db, _: Token
) -> FileAccepted:
    try:
        await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    file_id = uuid4()
    try:
        name = storage.safe_name(file.filename or str(file_id))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad filename") from exc
    target = storage.files_dir(session_id) / f"{file_id}_{name}"

    async def chunks() -> AsyncIterator[bytes]:
        while chunk := await file.read(storage.CHUNK):
            yield chunk

    size = await storage.write_atomic(target, chunks())
    async with db.tx() as tx:
        await tx.conn.execute(
            "insert into files (id, session_id, name, size) values (%s, %s, %s, %s)",
            (file_id, session_id, name, size),
        )
    return FileAccepted(file_id=file_id)


@router.get("/{session_id}/downloads/{name}")
async def get_download(session_id: UUID, name: str, _: Token) -> FileResponse:
    try:
        safe = storage.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad name") from exc
    path = storage.downloads_dir(session_id) / safe
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown download")
    return FileResponse(path, filename=path.name)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: UUID, sessions: Ss, db: Db, _: Token) -> None:
    try:
        state = await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    if state.status not in {SessionStatus.CLOSED, SessionStatus.DEAD}:
        raise HTTPException(status.HTTP_409_CONFLICT, "session is still running")
    async with db.tx() as tx:
        await tx.conn.execute("delete from sessions where id = %s", (session_id,))
    storage.remove_session(session_id)


@profiles_router.get("")
async def list_profiles(db: Db, _: Token) -> list[ProfileInfo]:
    async with db.conn() as conn:
        cur = await conn.execute("select * from profiles order by name")
        rows = await cur.fetchall()
    return [
        ProfileInfo(
            name=row["name"],
            size=row["size"],
            stale=row["stale"],
            updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        )
        for row in rows
    ]


@profiles_router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(name: str, db: Db, _: Token) -> None:
    try:
        safe = storage.safe_name(name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad name") from exc
    async with db.tx() as tx:
        await tx.conn.execute("delete from profiles where name = %s", (safe,))
    storage.profile_path(safe).unlink(missing_ok=True)
