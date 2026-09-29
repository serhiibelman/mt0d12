import asyncio
import contextlib
import os
import time

import pytest
from serial import SerialException

from conftest import settle
from lib.ddsm115 import BusBusy, BusUnavailable, MotorBus
from settings import LEFT_SIDE, RIGHT_SIDE

ALL_MOTORS = LEFT_SIDE + RIGHT_SIDE
STOP = [(motor_id, 0) for motor_id in ALL_MOTORS]


def sides(left: int, right: int) -> list[tuple[int, int]]:
    """What one command to each side puts on the wire: right side negated."""
    return [(m, left) for m in LEFT_SIDE] + [(m, -right) for m in RIGHT_SIDE]


# -- one port, many drivers ---------------------------------------------------


def test_the_port_opens_once_and_drivers_take_turns_on_it(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, motors):
            page, gamepad = object(), object()
            bus.claim(page, "A viewer")
            await bus.drive(page, 50, 30)
            await bus.release(page)
            bus.claim(gamepad, "The gamepad")
            await bus.drive(gamepad, 10, 10)

            assert len(motors) == 1
            assert motors[0].commands == sides(50, 30) + STOP + sides(10, 10)
            assert [m["rpm"] for m in bus.feedback()] == [10, 10, -10, -10]

    asyncio.run(main())


def test_one_holder_at_a_time_and_the_refusal_says_who(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, _):
            first, second = object(), object()
            bus.claim(first, "The gamepad")
            bus.claim(first, "The gamepad")  # holding it already is fine

            with pytest.raises(BusBusy, match="The gamepad is driving"):
                bus.claim(second, "A viewer")
            assert bus.component()["detail"] == "The gamepad is driving"

    asyncio.run(main())


def test_a_session_that_ended_cannot_touch_the_next_ones_bus(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, motors):
            old, new = object(), object()
            bus.claim(old, "old")
            await bus.release(old)
            bus.claim(new, "new")
            motors[0].commands.clear()

            await bus.drive(old, 100, 100)
            await bus.release(old)

            assert motors[0].commands == []
            assert bus.holder_name == "new"

    asyncio.run(main())


def test_a_release_without_a_stop_leaves_the_motors_turning(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, motors):
            ramp = object()
            bus.claim(ramp, "A ramp")
            await bus.drive(ramp, 40, 40)
            await bus.release(ramp, stop=False)

            assert motors[0].commands == sides(40, 40)
            assert bus.commanded_sides() == (40, 40)
            assert bus.holder_name is None

    asyncio.run(main())


def test_halt_stops_whoever_is_driving_and_refuses_everyone_after(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, motors):
            driver = object()
            bus.claim(driver, "A viewer")
            await bus.drive(driver, 80, 80)

            await bus.halt()

            assert motors[0].commands[-4:] == STOP
            with pytest.raises(BusUnavailable, match="shutting down"):
                bus.claim(object(), "The gamepad")

    asyncio.run(main())


def test_stopping_the_bus_stops_the_motors_and_closes_the_port(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, motors):
            ramp = object()
            bus.claim(ramp, "A ramp")
            await bus.drive(ramp, 40, 40)
            await bus.release(ramp, stop=False)
        # Leaving the block cancelled bus.run().
        assert motors[0].commands[-4:] == STOP
        assert motors[0].closed is True

    asyncio.run(main())


# -- the link -------------------------------------------------------------------


def test_a_failed_command_closes_the_port_and_it_reopens(running_bus) -> None:
    async def main():
        async with running_bus() as (bus, motors):
            driver = object()
            bus.claim(driver, "A viewer")
            motors[0].error = SerialException("device reports readiness but returned no data")

            with pytest.raises(BusUnavailable, match="no data"):
                await bus.drive(driver, 50, 50)

            await settle(lambda: len(motors) == 2 and bus.is_open)
            assert motors[0].closed is True
            # The driver still holds it, and is the one to let go.
            assert bus.holder_name == "A viewer"

    asyncio.run(main())


def test_an_adapter_unplugged_while_idle_is_noticed(running_bus) -> None:
    async def main():
        present = {"yes": True}
        async with running_bus(device_present=lambda _: present["yes"]) as (bus, motors):
            present["yes"] = False
            await settle(lambda: not bus.is_open)
            assert "/dev/test is gone" in bus.component()["detail"]
            assert bus.component()["connected"] is False

            present["yes"] = True
            await settle(lambda: bus.is_open)
            assert len(motors) == 2

    asyncio.run(main())


def test_a_port_that_will_not_open_is_retried_with_backoff() -> None:
    async def main():
        attempts = []

        def factory(*, device):
            attempts.append(time.monotonic())
            raise RuntimeError(f"Failed to open serial device {device}")

        bus = MotorBus(
            device="/dev/none", motor_factory=factory, backoff_min=0.02, backoff_max=0.04
        )
        task = asyncio.create_task(bus.run())
        await settle(lambda: len(attempts) >= 4)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        assert gaps[0] >= 0.02 and gaps[1] >= 0.04 and gaps[2] < 0.1  # doubling, capped
        assert "Failed to open serial device /dev/none" in bus.component()["detail"]
        with pytest.raises(BusUnavailable, match="Failed to open"):
            bus.claim(object(), "A viewer")

    asyncio.run(main())


def test_an_unconfigured_bus_says_so() -> None:
    async def main():
        bus = MotorBus(device=None)
        await bus.run()  # returns at once: nothing to open

        assert bus.component()["configured"] is False
        assert bus.component()["detail"] == "DEVICE is not configured"
        with pytest.raises(BusUnavailable, match="DEVICE is not configured"):
            bus.claim(object(), "A viewer")

    asyncio.run(main())


# -- the bus thread ---------------------------------------------------------------


def test_driving_never_queues_behind_the_shared_worker_pool(running_bus) -> None:
    # Everything else that blocks - FC reads, the camera opening, uvicorn's
    # sync routes - shares asyncio's default pool. Busy for a second here, and
    # a drive command still goes straight out on the bus's own thread.
    async def main():
        async with running_bus() as (bus, motors):
            driver = object()
            bus.claim(driver, "A viewer")
            # asyncio's default pool size, every worker taken and a queue behind.
            workers = min(32, (os.cpu_count() or 1) + 4)
            busy = [asyncio.to_thread(time.sleep, 1.0) for _ in range(workers + 4)]
            hogs = asyncio.gather(*busy)
            await asyncio.sleep(0.05)

            started = time.monotonic()
            await bus.drive(driver, 20, 20)
            took = time.monotonic() - started

            await hogs
            assert took < 0.2, f"a drive command waited {took:.2f}s for a worker"

    asyncio.run(main())


def test_a_stop_from_a_cancelled_task_still_goes_out_after_the_pass_on_the_bus(
    running_bus,
) -> None:
    async def main():
        async with running_bus(delay=0.01) as (bus, motors):
            driver = object()
            bus.claim(driver, "The gamepad")

            async def drive_then_let_go():
                try:
                    await bus.drive(driver, 30, 30)
                finally:
                    await bus.release(driver)

            task = asyncio.create_task(drive_then_let_go())
            await asyncio.sleep(0.015)  # the pass is on the bus
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await settle(lambda: len(motors[0].commands) == 8)

            # The pass finished - a thread cannot be cancelled - and the stop
            # came after it rather than between its writes.
            assert motors[0].commands == sides(30, 30) + STOP

    asyncio.run(main())
