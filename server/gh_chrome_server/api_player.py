from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, Response
from gh_chrome_protocol import SessionStatus

from gh_chrome_server import manifest, storage
from gh_chrome_server.auth import Basic
from gh_chrome_server.config import settings
from gh_chrome_server.deps import Ss
from gh_chrome_server.sessions import SessionNotFound

router = APIRouter(prefix="/s", tags=["player"])

PLAYER = Path(__file__).parent / "player"


@router.get("/{session_id}", response_class=HTMLResponse)
async def player_page(session_id: UUID, sessions: Ss, _: Basic) -> HTMLResponse:
    try:
        await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    html = (PLAYER / "index.html").read_text()
    return HTMLResponse(html.replace("{{SESSION_ID}}", str(session_id)))


@router.get("/{session_id}/dash.min.js")
async def player_script(session_id: UUID, _: Basic) -> FileResponse:
    return FileResponse(PLAYER / "dash.min.js", media_type="text/javascript")


@router.get("/{session_id}/manifest.mpd")
async def player_manifest(session_id: UUID, sessions: Ss, _: Basic) -> Response:
    try:
        state = await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
    directory = storage.segments_dir(session_id)
    segments = manifest.count_segments(directory)
    if segments == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no segments yet")
    live = state.status not in {SessionStatus.CLOSED, SessionStatus.DEAD}
    available_at = await sessions.started_at(session_id)
    xml = manifest.build(
        width=state.params.width,
        height=state.params.height,
        fps=state.params.fps,
        segment_seconds=settings.segment_seconds,
        segments=segments,
        available_at=available_at,
        live=live,
    )
    return Response(
        xml,
        media_type="application/dash+xml",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/{session_id}/init.m4s")
async def player_init(session_id: UUID, _: Basic) -> FileResponse:
    path = storage.segments_dir(session_id) / "init.m4s"
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no init segment")
    return FileResponse(path, media_type="video/mp4")


@router.get("/{session_id}/{number}.m4s")
async def player_segment(session_id: UUID, number: int, _: Basic) -> FileResponse:
    path = storage.segments_dir(session_id) / f"{number}.m4s"
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such segment")
    return FileResponse(path, media_type="video/mp4")
