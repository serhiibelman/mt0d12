from fastapi import APIRouter

from apps.api.dependencies import VehicleStatusServiceDep
from apps.api.schemas import VehicleHealthResponse

router = APIRouter()


@router.get("/health", response_model=VehicleHealthResponse)
async def health(service: VehicleStatusServiceDep) -> VehicleHealthResponse:
    snapshot = service.snapshot()
    return VehicleHealthResponse(
        status=snapshot["overall_status"],
        service=snapshot["service"],
        timestamp=snapshot["timestamp"],
        components=snapshot["components"],
    )
