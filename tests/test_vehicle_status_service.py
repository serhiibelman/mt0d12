import asyncio

import pytest

from apps.api.services.flight_controller import FlightControllerStream
from apps.api.services.vehicle_status import VehicleStatusService
from lib.ddsm115 import MotorBus


def make_service(bus: MotorBus, **kwargs) -> VehicleStatusService:
    async def no_pi() -> dict:
        return {}

    return VehicleStatusService(
        bus=bus,
        flight_controller=FlightControllerStream(device=None),
        ramp_interval=0,
        pi_health_reader=no_pi,
        **kwargs,
    )


# -- the /motors ramps, through the shared bus -------------------------------


def test_start_motors_ramps_all_motors(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, motors):
            service = make_service(bus)

            response = await service.start_motors(12)

            assert response["action"] == "start"
            assert response["current_rpm"] == 12
            assert motors[0].commands == [
                (3, 5),
                (4, 5),
                (1, -5),
                (2, -5),
                (3, 10),
                (4, 10),
                (1, -10),
                (2, -10),
                (3, 12),
                (4, 12),
                (1, -12),
                (2, -12),
            ]
            # Left turning, and nobody holding the bus.
            assert bus.holder_name is None

    asyncio.run(main())


def test_stop_motors_ramps_down_from_current_speed(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, motors):
            service = make_service(bus)
            await service.start_motors(12)
            motors[0].commands.clear()

            response = await service.stop_motors()

            assert response["action"] == "stop"
            assert response["current_rpm"] == 0
            assert motors[0].commands == [
                (3, 7),
                (4, 7),
                (1, -7),
                (2, -7),
                (3, 2),
                (4, 2),
                (1, -2),
                (2, -2),
                (3, 0),
                (4, 0),
                (1, 0),
                (2, 0),
            ]

    asyncio.run(main())


def test_a_ramp_is_refused_while_someone_drives(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, _):
            service = make_service(bus)
            bus.claim(object(), "The gamepad")

            with pytest.raises(RuntimeError, match="The gamepad is driving"):
                await service.start_motors(50)

    asyncio.run(main())


def test_the_snapshot_shows_the_bus_and_what_was_sent(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, _):
            service = make_service(bus)
            driver = object()
            bus.claim(driver, "A viewer on the status page")
            await bus.drive(driver, 60, 40)

            snapshot = service.snapshot()

            assert snapshot["components"]["motor_bus"]["connected"] is True
            assert snapshot["components"]["motor_bus"]["detail"] == (
                "A viewer on the status page is driving"
            )
            assert [m["rpm"] for m in snapshot["motor_feedback"]] == [60, 60, -40, -40]

    asyncio.run(main())


# -- the flight controller, through the service ---------------------------
# The stream itself is tested in test_flight_controller.py.


def test_an_unconfigured_flight_controller_reports_nothing_known() -> None:
    async def main():
        service = make_service(MotorBus(device=None))
        await service.probe_once()

        snapshot = service.snapshot()

        assert snapshot["battery"] == {
            "voltage_v": None,
            "current_a": None,
            "remaining_percent": None,
        }
        assert snapshot["attitude"] == {"roll_deg": None, "pitch_deg": None, "yaw_deg": None}
        assert snapshot["components"]["flight_controller"]["configured"] is False
        assert snapshot["overall_status"] == "degraded"

    asyncio.run(main())


def test_the_probe_reads_the_pi_until_cancelled() -> None:
    async def main():
        readings = []

        async def reader() -> dict:
            readings.append(1)
            return {"cpu_temp_c": 50.0 + len(readings)}

        service = make_service(MotorBus(device=None), probe_interval_seconds=0.01)
        service._read_pi_health = reader
        task = asyncio.create_task(service.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(readings) >= 3
        assert service.snapshot()["pi"]["cpu_temp_c"] == 50.0 + len(readings)

    asyncio.run(main())
