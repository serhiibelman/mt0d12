from fastapi import APIRouter, WebSocket

from apps.api.dependencies import StatusBroadcasterDep, VehicleStatusServiceDep
from apps.api.schemas import VehicleStatusResponse
from apps.api.streaming import forward_until_disconnect

router = APIRouter()


@router.get("/status", response_model=VehicleStatusResponse)
def status(service: VehicleStatusServiceDep) -> VehicleStatusResponse:
    return VehicleStatusResponse.from_snapshot(service.snapshot())


@router.websocket("/ws/status")
async def status_stream(websocket: WebSocket, broadcaster: StatusBroadcasterDep) -> None:
    """The /status snapshot, pushed as it changes; see `StatusBroadcaster`.

    `async def`, so every viewer is a coroutine on the event loop rather than a
    threadpool slot, and nothing in it may block.
    """
    await websocket.accept()
    with broadcaster.subscribe() as updates:
        await forward_until_disconnect(websocket, updates)
