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
from lib.telemetry import TelemetryPublisher


def create_app(
    vehicle_status_service: VehicleStatusService | None = None,
    camera_service: CameraService | None = None,
    telemetry_publisher: TelemetryPublisher | None = None,
    status_broadcaster: StatusBroadcaster | None = None,
) -> FastAPI:
    service = vehicle_status_service or VehicleStatusService()
    camera = camera_service or CameraService()
    # The publisher lives here rather than in its own process because this one
    # already owns the motor bus; a second process would fight for the serial
    # port. With no IOT_ENDPOINT configured it starts and stops as a no-op.
    telemetry = telemetry_publisher or TelemetryPublisher(snapshot=service.snapshot)
    # One producer behind /ws/status, rendering the same schema /status returns.
    broadcaster = status_broadcaster or StatusBroadcaster(
        render=lambda: VehicleStatusResponse.from_snapshot(service.snapshot()).json()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.vehicle_status_service = service
        app.state.camera_service = camera
        app.state.telemetry_publisher = telemetry
        app.state.status_broadcaster = broadcaster
        service.start()
        telemetry.start()
        # The camera opens on the first stream/snapshot request instead of at
        # boot, so the sensor stays powered down while nobody is watching.
        try:
            yield
        finally:
            await broadcaster.stop()
            telemetry.stop()
            service.stop()
            camera.stop()

    app = FastAPI(
        title="R2D2 Vehicle API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(camera_router)
    app.include_router(health_router)
    app.include_router(motors_router)
    app.include_router(pages_router)
    app.include_router(status_router)
    return app


app = create_app()
