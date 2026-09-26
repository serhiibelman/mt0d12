from fastapi import APIRouter

from apps.api.dependencies import VehicleStatusServiceDep
from apps.api.schemas import VehicleStatusResponse

router = APIRouter()


@router.get("/status", response_model=VehicleStatusResponse)
def status(service: VehicleStatusServiceDep) -> VehicleStatusResponse:
    snapshot = service.snapshot()
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
