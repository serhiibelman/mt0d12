import asyncio
import json
import logging
import socket
import time

from apps.vehicle_control.vehicle_controller import VehicleController
from conftest import settle
from lib.gamepad.udp_receiver import ControllerStateProtocol, open_receiver, parse_packet
from settings import LEFT_SIDE, RIGHT_SIDE

ALL_MOTORS = LEFT_SIDE + RIGHT_SIDE
STOP = [(motor_id, 0) for motor_id in ALL_MOTORS]
# Real timing, kept short: the loop is driven by the event loop's clock now.
TIMEOUT = 0.1


def datagram(*, a: bool = False, left_y: float = 0.0, lb: bool = False) -> bytes:
    buttons = {name: False for name in ("a", "b", "x", "y", "lb", "rb", "l", "r")}
    buttons["a"] = a
    buttons["lb"] = lb
    axes = {"left_x": 0.0, "left_y": left_y, "right_x": 0.0, "right_y": 0.0}
    axes.update(trigger_left=False, trigger_right=False)
    return json.dumps({"timestamp": 0.0, "axes": axes, "buttons": buttons}).encode()


class Rig:
    """A controller running on a protocol a test feeds by hand - no socket -
    driving a real `MotorBus` over a fake motor."""

    def __init__(self, bus, motor) -> None:
        self.bus = bus
        self.motor = motor
        self.packets = ControllerStateProtocol()
        self.controller = VehicleController(bus, link_timeout=TIMEOUT)

    def send(self, **fields) -> None:
        self.packets.datagram_received(datagram(**fields), ("127.0.0.1", 9))

    async def send_and_wait(self, **fields) -> None:
        """Send one packet and wait for its pass: four commands, one per motor."""
        done = len(self.motor.commands) + len(ALL_MOTORS)
        self.send(**fields)
        await settle(lambda: len(self.motor.commands) >= done)

    async def drive_forward(self) -> None:
        """Arm with 'a', then hold the stick forward long enough to build speed."""
        await self.send_and_wait(a=True)
        for _ in range(8):
            await self.send_and_wait(left_y=-1.0)
        assert self.controller._current_rpm == 40

    async def silence(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def run(scenario, running_bus, delay: float = 0.0) -> list:
    """Run `scenario(rig)` with the controller going; cancels it after, and
    returns every command the motor got."""
    commands: list = []

    async def main() -> None:
        async with running_bus(delay=delay) as (bus, motors):
            rig = Rig(bus, motors[0])
            task = asyncio.create_task(rig.controller.run(rig.packets))
            try:
                await scenario(rig)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                commands.extend(rig.motor.commands)

    asyncio.run(main())
    return commands


# -- link-drop stop ------------------------------------------------------------


def test_silence_past_timeout_stops_all_motors(running_bus) -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.motor.commands.clear()
        await rig.silence(TIMEOUT * 1.5)

        assert rig.motor.commands == STOP
        assert rig.controller._drive_enabled is False
        assert rig.controller._current_rpm == 0.0
        # And the bus is free for someone else.
        assert rig.bus.holder_name is None

    run(scenario, running_bus)


def test_short_gaps_do_not_stop(running_bus) -> None:
    async def scenario(rig):
        await rig.drive_forward()
        for _ in range(10):
            await asyncio.sleep(TIMEOUT / 3)
            rig.send(left_y=-1.0)
        await asyncio.sleep(0.01)

        assert rig.controller._drive_enabled is True
        assert rig.controller._current_rpm > 0

    run(scenario, running_bus)


def test_stop_is_sent_once_per_outage(running_bus) -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.motor.commands.clear()
        await rig.silence(TIMEOUT * 4)

        assert rig.motor.commands == STOP

    run(scenario, running_bus)


def test_no_failsafe_before_first_packet(running_bus) -> None:
    async def scenario(rig):
        await rig.silence(TIMEOUT * 3)
        assert rig.motor.commands == []

    run(scenario, running_bus)


def test_restored_link_stays_disarmed_until_a_is_pressed_again(running_bus) -> None:
    async def scenario(rig):
        await rig.drive_forward()
        await rig.silence(TIMEOUT * 1.5)

        # Link returns with the stick still pushed and 'a' still held down.
        rig.motor.commands.clear()
        rig.send(a=True, left_y=-1.0)
        await asyncio.sleep(0.02)
        assert rig.controller._drive_enabled is False
        # Disarmed, it does not hold the bus, so it sends nothing at all.
        assert rig.motor.commands == []

        # Release, then press: that is a deliberate re-arm.
        for a in (False, True):
            await asyncio.sleep(0.01)
            rig.send(a=a)
        await settle(lambda: rig.controller._drive_enabled)

    run(scenario, running_bus)


def test_second_outage_is_detected_after_recovery(running_bus) -> None:
    async def scenario(rig):
        await rig.drive_forward()
        await rig.silence(TIMEOUT * 1.5)
        for a in (False, True):
            await asyncio.sleep(0.01)
            rig.send(a=a)
        await settle(lambda: rig.controller._drive_enabled)
        await rig.send_and_wait(left_y=-1.0)

        rig.motor.commands.clear()
        await rig.silence(TIMEOUT * 1.5)
        assert rig.motor.commands == STOP

    run(scenario, running_bus)


def test_time_on_the_bus_counts_as_silence(running_bus) -> None:
    # The deadline runs from the last packet's arrival, not from when the loop
    # got round to waiting: a pass that held the bus has already used some.
    async def scenario(rig):
        await rig.send_and_wait(a=True)
        rig.motor.delay = TIMEOUT / 4  # a pass over 4 motors takes one TIMEOUT
        rig.send(left_y=-1.0)
        started = time.monotonic()
        await settle(lambda: not rig.controller._drive_enabled and rig.controller._link_lost)
        # One pass, then the stop - not a pass, a full timeout, and the stop.
        assert time.monotonic() - started < TIMEOUT * 2.5

    run(scenario, running_bus)


# -- sharing the bus -------------------------------------------------------------


def test_a_is_refused_while_someone_else_drives(running_bus) -> None:
    async def scenario(rig):
        page = object()
        rig.bus.claim(page, "A viewer on the status page")

        rig.send(a=True)
        await asyncio.sleep(0.02)
        rig.send(left_y=-1.0)
        await asyncio.sleep(0.02)

        assert rig.controller._drive_enabled is False
        assert rig.motor.commands == []
        assert rig.bus.holder_name == "A viewer on the status page"

        # Once the page lets go, 'a' works.
        await rig.bus.release(page)
        for a in (False, True):
            rig.send(a=a)
            await asyncio.sleep(0.01)
        await settle(lambda: rig.controller._drive_enabled)
        assert rig.bus.holder_name == "The gamepad"

    run(scenario, running_bus)


def test_drive_off_ramps_down_then_lets_the_bus_go(running_bus) -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.send()
        await asyncio.sleep(0.01)
        await rig.send_and_wait(a=True)  # drive off
        while rig.controller._current_rpm:
            await rig.send_and_wait()

        await settle(lambda: rig.bus.holder_name is None)
        assert rig.motor.commands[-4:] == STOP

    run(scenario, running_bus)


def test_brake_brakes_and_lets_the_bus_go(running_bus) -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.motor.commands.clear()

        await rig.send_and_wait(lb=True)

        assert rig.motor.commands == [(motor_id, "brake") for motor_id in ALL_MOTORS]
        assert rig.controller._drive_enabled is False
        assert rig.bus.holder_name is None

    run(scenario, running_bus)


# -- the bus off the event loop ----------------------------------------------


def test_packets_that_land_during_a_pass_collapse_to_the_newest(running_bus) -> None:
    async def scenario(rig):
        await rig.send_and_wait(a=True)
        rig.motor.delay = 0.01  # 40 ms a pass
        rig.controller.link_timeout = 10  # only the passes under test, no stop
        rig.motor.commands.clear()
        rig.send(left_y=-1.0)
        await asyncio.sleep(0.005)  # that pass is on the bus now
        for _ in range(5):
            rig.send(left_y=-1.0)
        await asyncio.sleep(0.2)
        # Two passes - the first, then the newest of the five - not six.
        assert len(rig.motor.commands) == 2 * len(ALL_MOTORS)

    run(scenario, running_bus)


# What PYTHONASYNCIODEBUG=1 reports: any step over slow_callback_duration. The
# motors take 100 ms each here, as one that does not answer does, so a pass on
# the loop is one 400 ms step. The threshold sits at half that: a shared CI
# runner has been seen to stretch an ordinary step to 50 ms, and that is not
# what this is looking for.
SLOW_STEP = 0.2


def slow_steps_while_driving(running_bus, on_the_loop=False, drive_packets: int = 6) -> list[str]:
    """Drive a few passes over a 100 ms-per-motor bus in debug mode, and
    return the slow-step warnings asyncio logged."""
    slow_steps: list[str] = []

    class Catch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "took" in record.getMessage():
                slow_steps.append(record.getMessage())

    async def main() -> None:
        async with running_bus(delay=0.1) as (bus, motors):
            asyncio.get_running_loop().slow_callback_duration = SLOW_STEP
            if on_the_loop:
                # As before the bus had a thread: every call straight from the loop.
                async def direct(call, *args, **kwargs):
                    return call(*args, **kwargs)

                bus._call = direct
            rig = Rig(bus, motors[0])
            task = asyncio.create_task(rig.controller.run(rig.packets))
            rig.send(a=True)
            for _ in range(drive_packets):
                await asyncio.sleep(0.05)
                rig.send(left_y=-1.0)
            await settle(lambda: len(rig.motor.commands) >= 4)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    handler = Catch()
    logging.getLogger("asyncio").addHandler(handler)
    try:
        asyncio.run(main(), debug=True)
    finally:
        logging.getLogger("asyncio").removeHandler(handler)
    return slow_steps


def test_a_slow_bus_never_stalls_the_event_loop(running_bus) -> None:
    assert slow_steps_while_driving(running_bus) == []


def test_the_stall_check_does_catch_a_bus_on_the_loop(running_bus) -> None:
    # The control for the test above: with the bus called straight from the
    # loop, the same run must be reported - or the threshold has drifted to
    # where the check can no longer fail. One blocked pass is proof enough.
    assert slow_steps_while_driving(running_bus, on_the_loop=True, drive_packets=1) != []


def test_cancelling_mid_pass_still_ends_with_the_motors_stopped(running_bus) -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.motor.delay = 0.02
        rig.send(left_y=-1.0)
        await asyncio.sleep(0.01)  # a pass is on the bus; the run() is cancelled now

    commands = run(scenario, running_bus)
    # The pass finished first - a thread cannot be cancelled - and the stop
    # came after it rather than between its writes.
    assert commands[-len(ALL_MOTORS) :] == STOP
    driving = commands[-2 * len(ALL_MOTORS) : -len(ALL_MOTORS)]
    assert all(rpm != 0 for _, rpm in driving)


# -- packets -------------------------------------------------------------------


def test_bad_packets_are_dropped() -> None:
    for junk in (b"{", b"\xff\xfe", b"[]", b'{"timestamp": 0}', b"null"):
        assert parse_packet(junk) is None
    extra = json.loads(datagram())
    extra["axes"]["unknown"] = 1
    assert parse_packet(json.dumps(extra).encode()) is None


def test_a_bad_packet_is_not_a_sign_of_life(running_bus) -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.motor.commands.clear()
        for _ in range(6):
            await asyncio.sleep(TIMEOUT / 3)
            rig.packets.datagram_received(b"garbage", ("127.0.0.1", 9))
        assert rig.motor.commands == STOP

    run(scenario, running_bus)


def test_packets_arrive_over_a_real_socket() -> None:
    async def main() -> None:
        transport, packets = await open_receiver("127.0.0.1", 0)
        port = transport.get_extra_info("sockname")[1]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(datagram(a=True), ("127.0.0.1", port))
                async with asyncio.timeout(1):
                    state = await packets.next_state()
        finally:
            transport.close()
        assert state.buttons.a is True

    asyncio.run(main())
