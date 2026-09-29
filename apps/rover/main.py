"""
Start the rover: `python -m apps.rover.main`, or `./start_rover.sh`.

The one process on the vehicle. It replaces the API (`uvicorn apps.api.main`)
and `apps.vehicle_control.main`, which each opened the motor bus and could
not run together; see `apps/rover/rover.py`.
"""

import asyncio
import logging
import signal

from apps.api.main import create_app
from apps.api.services.camera import CameraService
from apps.api.services.flight_controller import FlightControllerStream
from apps.api.services.vehicle_status import VehicleStatusService
from apps.rover.rover import ApiServer, Rover
from apps.vehicle_control.vehicle_controller import VehicleController
from lib.ddsm115 import MotorBus
from lib.gamepad.udp_receiver import DEFAULT_PORT as GAMEPAD_PORT
from lib.gamepad.udp_receiver import open_receiver
from lib.telemetry import TelemetryPublisher

HOST = "0.0.0.0"
PORT = 8000


def build() -> Rover:
    bus = MotorBus()
    flight_controller = FlightControllerStream()
    status = VehicleStatusService(bus=bus, flight_controller=flight_controller)
    camera = CameraService()

    async def gamepad() -> None:
        transport, packets = await open_receiver(HOST, GAMEPAD_PORT)
        try:
            await VehicleController(bus).run(packets)
        finally:
            transport.close()

    return Rover(
        bus=bus,
        flight_controller=flight_controller,
        status=status,
        camera=camera,
        # With no IOT_ENDPOINT configured its run returns at once.
        telemetry=TelemetryPublisher(snapshot=status.snapshot),
        api=ApiServer(
            create_app(vehicle_status_service=status, camera_service=camera),
            host=HOST,
            port=PORT,
        ),
        gamepad=gamepad,
    )


async def run() -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    await build().run(stopping)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
