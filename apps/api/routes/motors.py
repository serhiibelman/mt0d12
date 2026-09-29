from fastapi import APIRouter, HTTPException, status

from apps.api.dependencies import VehicleStatusServiceDep
from apps.api.schemas import MotorCommandResponse, StartMotorsRequest

router = APIRouter(prefix="/motors", tags=["motors"])


@router.post("/start", response_model=MotorCommandResponse)
async def start_motors(
    service: VehicleStatusServiceDep, payload: StartMotorsRequest
) -> MotorCommandResponse:
    try:
        result = await service.start_motors(payload.rpm)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return MotorCommandResponse(**result)


@router.post("/stop", response_model=MotorCommandResponse)
async def stop_motors(service: VehicleStatusServiceDep) -> MotorCommandResponse:
    try:
        result = await service.stop_motors()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return MotorCommandResponse(**result)
