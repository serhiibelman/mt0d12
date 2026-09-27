"""Typed access to the services `create_app` put on `app.state`.

Routes ask for what they need instead of reaching into `request.app.state`, so
the lookup is checked and a handler's signature says what it depends on.

The getters take an `HTTPConnection` - the common base of `Request` and
`WebSocket` - so the same dependency serves HTTP routes and WebSocket routes.
"""

from typing import Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from apps.api.services.camera import CameraService
from apps.api.services.status_broadcaster import StatusBroadcaster
from apps.api.services.vehicle_status import VehicleStatusService


def get_vehicle_status_service(connection: HTTPConnection) -> VehicleStatusService:
    return connection.app.state.vehicle_status_service


def get_camera_service(connection: HTTPConnection) -> CameraService:
    return connection.app.state.camera_service


def get_status_broadcaster(connection: HTTPConnection) -> StatusBroadcaster:
    return connection.app.state.status_broadcaster


VehicleStatusServiceDep = Annotated[VehicleStatusService, Depends(get_vehicle_status_service)]
CameraServiceDep = Annotated[CameraService, Depends(get_camera_service)]
StatusBroadcasterDep = Annotated[StatusBroadcaster, Depends(get_status_broadcaster)]
