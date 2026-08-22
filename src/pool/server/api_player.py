from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, Response

from pool.server import manifest, storage
from pool.server.auth import Basic, hand_out_ticket
from pool.server.config import settings
from pool.server.deps import Ss

router = APIRouter(prefix="/s", tags=["player"])

PAGE = (Path(__file__).parent / "player" / "index.html").read_text()


@router.get("/{session_id}", response_class=HTMLResponse)
async def player_page(
    session_id: UUID, request: Request, sessions: Ss, _: Basic
) -> HTMLResponse:
    await sessions.get(session_id)
    page = HTMLResponse(PAGE.replace("{{SESSION_ID}}", str(session_id)))
    hand_out_ticket(page, request, session_id)
    return page


@router.get("/{session_id}/manifest.mpd")
async def player_manifest(session_id: UUID, sessions: Ss, _: Basic) -> Response:
    state = await sessions.get(session_id)
    segments = manifest.count_segments(storage.segments_dir(session_id))
    if segments == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no segments yet")
    xml = manifest.build(
        width=state.params.width,
        height=state.params.height,
        fps=state.params.fps,
        segment_seconds=settings.segment_seconds,
        segments=segments,
        available_at=await sessions.started_at(session_id),
        live=state.status.live,
    )
    return Response(
        xml, media_type="application/dash+xml", headers={"Cache-Control": "no-cache"}
    )


@router.get("/{session_id}/init.m4s")
async def player_init(session_id: UUID, _: Basic) -> FileResponse:
    return _segment(storage.segments_dir(session_id) / "init.m4s")


@router.get("/{session_id}/{number}.m4s")
async def player_segment(session_id: UUID, number: int, _: Basic) -> FileResponse:
    return _segment(storage.segments_dir(session_id) / f"{number}.m4s")


def _segment(path: Path) -> FileResponse:
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such segment")
    return FileResponse(path, media_type="video/mp4")
