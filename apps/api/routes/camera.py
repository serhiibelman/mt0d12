from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

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
def stream(request: Request, service: CameraServiceDep) -> StreamingResponse:
    try:
        service.acquire_client_slot()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return StreamingResponse(
        mjpeg_parts(request, service),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers=NO_CACHE_HEADERS,
    )


@router.get("/snapshot")
def snapshot(service: CameraServiceDep) -> Response:
    try:
        frame = service.capture_frame()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return Response(content=frame, media_type="image/jpeg", headers=NO_CACHE_HEADERS)


@router.get("/status", response_model=CameraStatusResponse)
def camera_status(service: CameraServiceDep) -> CameraStatusResponse:
    return CameraStatusResponse(**service.snapshot())


@router.post("/start", response_model=CameraCommandResponse)
def start_camera(service: CameraServiceDep) -> CameraCommandResponse:
    try:
        result = service.start()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return CameraCommandResponse(**result)


@router.post("/stop", response_model=CameraCommandResponse)
def stop_camera(service: CameraServiceDep) -> CameraCommandResponse:
    try:
        result = service.stop()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return CameraCommandResponse(**result)
