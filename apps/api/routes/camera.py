from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from apps.api.dependencies import CameraServiceDep
from apps.api.schemas import CameraCommandResponse, CameraStatusResponse
from apps.api.streaming import MJPEG_BOUNDARY, mjpeg_parts

router = APIRouter(prefix="/camera", tags=["camera"])

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Age": "0",
}


@router.get("/stream")
async def stream(service: CameraServiceDep) -> StreamingResponse:
    """`async def`, so a viewer waits for frames as a coroutine; only opening
    the camera, which blocks for a second or so, goes to a thread."""
    try:
        await service.acquire_client_slot()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return StreamingResponse(
        mjpeg_parts(service),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers=NO_CACHE_HEADERS,
        # Runs once streaming ends for any reason, the viewer leaving included.
        background=BackgroundTask(service.release_client_slot),
    )


@router.get("/snapshot")
async def snapshot(service: CameraServiceDep) -> Response:
    try:
        frame = await service.capture_frame()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return Response(content=frame, media_type="image/jpeg", headers=NO_CACHE_HEADERS)


@router.get("/status", response_model=CameraStatusResponse)
async def camera_status(service: CameraServiceDep) -> CameraStatusResponse:
    return CameraStatusResponse(**service.snapshot())


@router.post("/start", response_model=CameraCommandResponse)
async def start_camera(service: CameraServiceDep) -> CameraCommandResponse:
    try:
        result = await service.start()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return CameraCommandResponse(**result)


@router.post("/stop", response_model=CameraCommandResponse)
async def stop_camera(service: CameraServiceDep) -> CameraCommandResponse:
    try:
        result = await service.stop()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return CameraCommandResponse(**result)
