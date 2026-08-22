from uuid import UUID

from fastapi import APIRouter, WebSocket

from gh_pool.core.auth import SocketRunner
from gh_pool.relay.deps import Tn

router = APIRouter(prefix="/runner", tags=["runner"])


@router.websocket("/{session_id}/tunnel")
async def serve_tunnel(
    session_id: UUID, websocket: WebSocket, tunnels: Tn, _: SocketRunner
) -> None:
    await websocket.accept()
    await tunnels.serve(session_id, websocket)
