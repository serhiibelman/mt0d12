import asyncio
import json
import logging
import socket
import time

from apps.vehicle_control.vehicle_controller import VehicleController
from lib.gamepad.udp_receiver import ControllerStateProtocol, open_receiver, parse_packet
from settings import LEFT_SIDE, RIGHT_SIDE

ALL_MOTORS = LEFT_SIDE + RIGHT_SIDE
STOP = [(motor_id, 0) for motor_id in ALL_MOTORS]
# Real timing, kept short: the loop is driven by the event loop's clock now.
TIMEOUT = 0.1


class FakeMotor:
    """Records commands; `delay` makes each one hold the bus like a real motor."""

    def __init__(self, delay: float = 0.0) -> None:
        self.commands: list[tuple[int, object]] = []
        self.delay = delay

    def send_rpm(self, motor_id: int, rpm: int = 0) -> None:
        time.sleep(self.delay)
        self.commands.append((motor_id, rpm))

    def set_brake(self, motor_id: int) -> None:
        time.sleep(self.delay)
        self.commands.append((motor_id, "brake"))


def datagram(*, a: bool = False, left_y: float = 0.0) -> bytes:
    buttons = {name: False for name in ("a", "b", "x", "y", "lb", "rb", "l", "r")}
    buttons["a"] = a
    axes = {"left_x": 0.0, "left_y": left_y, "right_x": 0.0, "right_y": 0.0}
    axes.update(trigger_left=False, trigger_right=False)
    return json.dumps({"timestamp": 0.0, "axes": axes, "buttons": buttons}).encode()


class Rig:
    """A controller running on a protocol a test feeds by hand - no socket."""

    def __init__(self, motor: FakeMotor) -> None:
        self.motor = motor
        self.packets = ControllerStateProtocol()
        self.controller = VehicleController(motor, link_timeout=TIMEOUT)

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


async def settle(condition, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.002)


def run(scenario, motor: FakeMotor | None = None) -> FakeMotor:
    """Run `scenario(rig)` with the controller going; cancels it after."""
    motor = motor or FakeMotor()

    async def main() -> None:
        rig = Rig(motor)
        task = asyncio.create_task(rig.controller.run(rig.packets))
        try:
            await scenario(rig)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(main())
    return motor


# -- link-drop stop ------------------------------------------------------------


def test_silence_past_timeout_stops_all_motors() -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.motor.commands.clear()
        await rig.silence(TIMEOUT * 1.5)

        assert rig.motor.commands == STOP
        assert rig.controller._drive_enabled is False
        assert rig.controller._current_rpm == 0.0

    run(scenario)


def test_short_gaps_do_not_stop() -> None:
    async def scenario(rig):
        await rig.drive_forward()
        for _ in range(10):
            await asyncio.sleep(TIMEOUT / 3)
            rig.send(left_y=-1.0)
        await asyncio.sleep(0.01)

        assert rig.controller._drive_enabled is True
        assert rig.controller._current_rpm > 0

    run(scenario)


def test_stop_is_sent_once_per_outage() -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.motor.commands.clear()
        await rig.silence(TIMEOUT * 4)

        assert rig.motor.commands == STOP

    run(scenario)


def test_no_failsafe_before_first_packet() -> None:
    async def scenario(rig):
        await rig.silence(TIMEOUT * 3)
        assert rig.motor.commands == []

    run(scenario)


def test_restored_link_stays_disarmed_until_a_is_pressed_again() -> None:
    async def scenario(rig):
        await rig.drive_forward()
        await rig.silence(TIMEOUT * 1.5)

        # Link returns with the stick still pushed and 'a' still held down.
        rig.motor.commands.clear()
        rig.send(a=True, left_y=-1.0)
        await settle(lambda: rig.motor.commands)
        assert rig.controller._drive_enabled is False
        assert all(rpm == 0 for _, rpm in rig.motor.commands)

        # Release, then press: that is a deliberate re-arm.
        for a in (False, True):
            await asyncio.sleep(0.01)
            rig.send(a=a)
        await settle(lambda: rig.controller._drive_enabled)

    run(scenario)


def test_second_outage_is_detected_after_recovery() -> None:
    async def scenario(rig):
        await rig.drive_forward()
        await rig.silence(TIMEOUT * 1.5)
        rig.send()
        await asyncio.sleep(0.01)

        rig.motor.commands.clear()
        await rig.silence(TIMEOUT * 1.5)
        assert rig.motor.commands == STOP

    run(scenario)


def test_time_on_the_bus_counts_as_silence() -> None:
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

    run(scenario)


# -- the bus off the event loop ----------------------------------------------


def test_packets_that_land_during_a_pass_collapse_to_the_newest() -> None:
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

    run(scenario)


def test_a_slow_bus_never_stalls_the_event_loop() -> None:
    """What PYTHONASYNCIODEBUG=1 checks: no step over slow_callback_duration.

    The motors take 100 ms each here, as one that does not answer does. Called
    on the loop, every pass would be a 400 ms step.
    """
    slow_steps: list[str] = []

    class Catch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "took" in record.getMessage():
                slow_steps.append(record.getMessage())

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = 0.05
        rig = Rig(FakeMotor(delay=0.1))
        task = asyncio.create_task(rig.controller.run(rig.packets))
        rig.send(a=True)
        for _ in range(6):
            await asyncio.sleep(0.05)
            rig.send(left_y=-1.0)
        await settle(lambda: len(rig.motor.commands) >= 8)
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

    assert slow_steps == []


def test_cancelling_mid_pass_still_ends_with_the_motors_stopped() -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.motor.delay = 0.02
        rig.send(left_y=-1.0)
        await asyncio.sleep(0.01)  # a pass is on the bus; the run() is cancelled now

    motor = run(scenario)
    # The pass finished first - a thread cannot be cancelled - and the stop,
    # waiting on the bus lock, came after it rather than between its writes.
    assert motor.commands[-len(ALL_MOTORS) :] == STOP
    driving = motor.commands[-2 * len(ALL_MOTORS) : -len(ALL_MOTORS)]
    assert all(rpm != 0 for _, rpm in driving)


# -- packets -------------------------------------------------------------------


def test_bad_packets_are_dropped() -> None:
    for junk in (b"{", b"\xff\xfe", b"[]", b'{"timestamp": 0}', b"null"):
        assert parse_packet(junk) is None
    extra = json.loads(datagram())
    extra["axes"]["unknown"] = 1
    assert parse_packet(json.dumps(extra).encode()) is None


def test_a_bad_packet_is_not_a_sign_of_life() -> None:
    async def scenario(rig):
        await rig.drive_forward()
        rig.motor.commands.clear()
        for _ in range(6):
            await asyncio.sleep(TIMEOUT / 3)
            rig.packets.datagram_received(b"garbage", ("127.0.0.1", 9))
        assert rig.motor.commands == STOP

    run(scenario)


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
