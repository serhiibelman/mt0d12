import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket

from apps.api.dependencies import VehicleStatusServiceDep
from apps.api.schemas import VehicleStatusResponse

router = APIRouter()

# 5 Hz: live enough to watch the rover tilt, and on a Pi 1 each message is one
# snapshot copy plus a few hundred bytes of JSON per viewer.
STATUS_STREAM_INTERVAL = 0.2


def status_response(snapshot: dict[str, Any]) -> VehicleStatusResponse:
    """The snapshot as the public schema. Shared by /status and /ws/status so
    both always say the same thing - and the model is what turns datetimes
    into JSON, which plain json.dumps cannot."""
    return VehicleStatusResponse(
        service=snapshot["service"],
        timestamp=snapshot["timestamp"],
        motor_device=snapshot["motor_device"],
        fc_device=snapshot["fc_device"],
        motor_ids=snapshot["motor_ids"],
        components=snapshot["components"],
        battery=snapshot["battery"],
        attitude=snapshot["attitude"],
        pi=snapshot["pi"],
        motor_feedback=snapshot["motor_feedback"],
    )


@router.get("/status", response_model=VehicleStatusResponse)
def status(service: VehicleStatusServiceDep) -> VehicleStatusResponse:
    return status_response(service.snapshot())


@router.websocket("/ws/status")
async def status_stream(websocket: WebSocket, service: VehicleStatusServiceDep) -> None:
    """Push the /status snapshot to the viewer every STATUS_STREAM_INTERVAL.

    `async def`, so this runs on the event loop itself rather than in the
    threadpool: every viewer is one coroutine on one thread, and nothing here
    may block. `service.snapshot()` is safe to call directly - it copies dicts
    under a lock and never waits on hardware; the probe thread does that.
    """
    await websocket.accept()
    loop = asyncio.get_running_loop()

    while True:
        await websocket.send_text(status_response(service.snapshot()).json())
        next_send = loop.time() + STATUS_STREAM_INTERVAL

        # Wait out the interval, but listen while waiting instead of sleeping:
        # a closing browser sends a disconnect message, and this is where it
        # arrives. A plain asyncio.sleep() would only find out on the next
        # failed send. Anything else the client sends is ignored for now - the
        # deadline keeps a chatty client from speeding the stream up.
        while (remaining := next_send - loop.time()) > 0:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if message["type"] == "websocket.disconnect":
                return
