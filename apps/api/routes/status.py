import json

from fastapi import APIRouter, WebSocket

from apps.api.dependencies import (
    DriveLinkTimeoutDep,
    StatusBroadcasterDep,
    VehicleStatusServiceDep,
)
from apps.api.schemas import VehicleStatusResponse
from apps.api.services.drive import DriveSession
from apps.api.streaming import locked_sender, serve_until_disconnect

router = APIRouter()


@router.get("/status", response_model=VehicleStatusResponse)
def status(service: VehicleStatusServiceDep) -> VehicleStatusResponse:
    return VehicleStatusResponse.from_snapshot(service.snapshot())


@router.websocket("/ws/status")
async def status_stream(
    websocket: WebSocket,
    broadcaster: StatusBroadcasterDep,
    service: VehicleStatusServiceDep,
    link_timeout: DriveLinkTimeoutDep,
) -> None:
    """
    The /status snapshot, pushed as it changes; see `StatusBroadcaster`.
    The same socket takes drive commands back; see `DriveSession`.

    `async def`, so every viewer is a coroutine on the event loop rather than a
    threadpool slot, and nothing in it may block.
    """
    await websocket.accept()
    send = locked_sender(websocket)
    session = DriveSession(service, notify=lambda state: send(json.dumps(state)))
    try:
        with broadcaster.subscribe() as updates:
            await serve_until_disconnect(websocket, send, updates, session, link_timeout)
    finally:
        # However the connection ended - the viewer left, a send failed, or
        # the server is shutting down - a driver's motors stop here.
        await session.close()
