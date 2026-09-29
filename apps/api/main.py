"""
The web side of the rover: routes over services the rover owns.

The app builds none of its services and starts none of them. `apps/rover`
creates them, runs them under its supervisor and stops them in order; the
app only serves them, as one more task in the same process. So a viewer, the
gamepad and the telemetry all see the same motor bus and the same readings.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.routes.camera import router as camera_router
from apps.api.routes.health import router as health_router
from apps.api.routes.motors import router as motors_router
from apps.api.routes.pages import router as pages_router
from apps.api.routes.status import router as status_router
from apps.api.schemas import VehicleStatusResponse
from apps.api.services.camera import CameraService
from apps.api.services.status_broadcaster import StatusBroadcaster
from apps.api.services.vehicle_status import VehicleStatusService
from apps.vehicle_control.vehicle_controller import LINK_TIMEOUT


def create_app(
    *,
    vehicle_status_service: VehicleStatusService,
    camera_service: CameraService,
    status_broadcaster: StatusBroadcaster | None = None,
    drive_link_timeout: float = LINK_TIMEOUT,
) -> FastAPI:
    service = vehicle_status_service
    # One producer behind /ws/status, rendering the same schema /status returns.
    broadcaster = status_broadcaster or StatusBroadcaster(
        render=lambda: VehicleStatusResponse.from_snapshot(service.snapshot()).json()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.vehicle_status_service = service
        app.state.camera_service = camera_service
        app.state.status_broadcaster = broadcaster
        # How long a driver may go without a command before the motors stop.
        app.state.drive_link_timeout = drive_link_timeout
        try:
            yield
        finally:
            await broadcaster.stop()

    app = FastAPI(
        title="MT0D12 Vehicle API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(camera_router)
    app.include_router(health_router)
    app.include_router(motors_router)
    app.include_router(pages_router)
    app.include_router(status_router)
    return app
