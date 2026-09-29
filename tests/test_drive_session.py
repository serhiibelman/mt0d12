import asyncio
import json
import time

from apps.api.services.drive import DriveSession, axis
from apps.api.streaming import _read_commands


class FakeBus:
    """The motor bus, with a gate a test can close to hold a command on it."""

    def __init__(self) -> None:
        self.owner: object | None = None
        self.commands: list[tuple[int, int]] = []
        self.closes = 0
        self.fail_claim: str | None = None
        self.fail_drive: str | None = None
        self.gate = asyncio.Event()
        self.gate.set()

    def claim(self, owner: object, name: str) -> None:
        if self.fail_claim:
            raise RuntimeError(self.fail_claim)
        self.owner = owner

    async def drive(self, owner: object, left_rpm: int, right_rpm: int) -> None:
        await self.gate.wait()
        if self.fail_drive:
            raise RuntimeError(self.fail_drive)
        if owner is self.owner:
            self.commands.append((left_rpm, right_rpm))

    async def release(self, owner: object, *, stop: bool = True) -> None:
        if self.owner is not None and owner is self.owner:
            self.owner = None
            self.closes += 1


class Notes(list):
    async def __call__(self, state: dict) -> None:
        self.append(state)


async def settle(condition, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.001)


def run_session(scenario):
    """Run `scenario(session, bus, notes)` with the motor task going."""

    async def main() -> None:
        bus, notes = FakeBus(), Notes()
        session = DriveSession(bus, notes)
        motors = asyncio.create_task(session.run_motors())
        try:
            await scenario(session, bus, notes)
        finally:
            motors.cancel()
            await session.close()

    asyncio.run(main())


def test_axis_accepts_numbers_and_clamps_them() -> None:
    assert axis(0.5) == 0.5
    assert axis(3) == 1.0
    assert axis(-7.5) == -1.0
    for bad in (None, "1", True, float("nan"), float("inf"), [1]):
        assert axis(bad) is None


def test_commands_before_arming_move_nothing() -> None:
    async def scenario(session, bus, notes):
        session.command(1.0, 0.0)
        await asyncio.sleep(0.01)
        assert bus.commands == []

    run_session(scenario)


def test_throttle_ramps_like_the_gamepad() -> None:
    async def scenario(session, bus, notes):
        await session.arm()
        assert notes == [{"type": "drive", "armed": True, "detail": "Driving"}]
        for expected in (5, 10, 15):
            session.command(1.0, 0.0)
            await settle(lambda: len(bus.commands) == expected // 5)
        assert bus.commands == [(5, 5), (10, 10), (15, 15)]

    run_session(scenario)


def test_steering_alone_turns_on_the_spot() -> None:
    async def scenario(session, bus, notes):
        await session.arm()
        session.command(0.0, 1.0)
        await settle(lambda: bus.commands)
        assert bus.commands == [(100, -100)]

    run_session(scenario)


def test_positions_that_arrive_while_the_bus_is_busy_collapse_to_the_newest() -> None:
    async def scenario(session, bus, notes):
        await session.arm()
        bus.gate.clear()
        session.command(0.0, 0.2)  # taken, and stuck on the bus
        await asyncio.sleep(0.01)
        for steer in (0.3, 0.4, 0.5):
            session.command(0.0, steer)
        bus.gate.set()
        await settle(lambda: len(bus.commands) == 2)
        await asyncio.sleep(0.01)
        # 0.3 and 0.4 were superseded before the bus was free.
        assert bus.commands == [(20, -20), (50, -50)]

    run_session(scenario)


def test_disarming_stops_and_needs_a_fresh_arm() -> None:
    async def scenario(session, bus, notes):
        await session.arm()
        await session.disarm("Stopped")
        assert bus.closes == 1
        assert notes[-1] == {"type": "drive", "armed": False, "detail": "Stopped"}
        session.command(1.0, 0.0)
        await asyncio.sleep(0.01)
        assert bus.commands == []

    run_session(scenario)


def test_rearming_starts_from_standstill() -> None:
    async def scenario(session, bus, notes):
        await session.arm()
        for _ in range(3):
            session.command(1.0, 0.0)
            await asyncio.sleep(0.005)
        await session.disarm("Stopped")
        await session.arm()
        bus.commands.clear()
        session.command(1.0, 0.0)
        await settle(lambda: bus.commands)
        assert bus.commands == [(5, 5)]

    run_session(scenario)


def test_a_refused_arm_says_why() -> None:
    async def scenario(session, bus, notes):
        bus.fail_claim = "The gamepad is driving"
        await session.arm()
        assert session.armed is False
        assert notes == [{"type": "drive", "armed": False, "detail": "The gamepad is driving"}]

    run_session(scenario)


def test_a_failing_bus_disarms() -> None:
    async def scenario(session, bus, notes):
        await session.arm()
        bus.fail_drive = "write timeout"
        session.command(1.0, 0.0)
        await settle(lambda: not session.armed)
        assert bus.closes == 1
        assert notes[-1]["detail"] == "Motor bus failed: write timeout"

    run_session(scenario)


def test_close_releases_the_bus_however_the_session_ends() -> None:
    async def scenario(session, bus, notes):
        await session.arm()
        session.command(1.0, 0.0)
        await settle(lambda: bus.commands)
        # No disarm, no notice to the page: the connection just ended.
        await session.close()
        assert bus.owner is None
        assert bus.closes == 1
        assert notes == [{"type": "drive", "armed": True, "detail": "Driving"}]

    run_session(scenario)


class QueueSocket:
    """Just enough WebSocket for `_read_commands`: receive from a queue."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[dict] = asyncio.Queue()

    async def receive(self) -> dict:
        return await self.inbox.get()

    def put(self, command: dict) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(command)})


def test_a_stalled_event_loop_is_not_taken_for_a_lost_link() -> None:
    async def scenario(session, bus, notes):
        socket = QueueSocket()
        await session.arm()
        reader = asyncio.create_task(_read_commands(socket, session, 0.05))
        await asyncio.sleep(0)
        # A command that arrives while the loop is blocked comes due in the
        # same pass as the link timeout, and after it - as when the camera
        # opening holds the Pi's only core.
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, socket.put, {"type": "drive", "throttle": 0.5, "steer": 0})
        time.sleep(0.2)
        await asyncio.sleep(0.02)
        reader.cancel()
        assert session.armed, notes

    run_session(scenario)
