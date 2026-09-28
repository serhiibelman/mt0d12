import asyncio
import queue
import threading
import time

from apps.api.services.flight_controller import FlightControllerStream
from apps.api.services.vehicle_status import VehicleStatusService

# Real time, kept short.
STALE = 0.15
RECV = 0.02


class FakeMessage:
    def __init__(self, kind: str, **fields):
        self._kind = kind
        for name, value in fields.items():
            setattr(self, name, value)

    def get_type(self) -> str:
        return self._kind

    def get_srcSystem(self) -> int:
        return 1


def heartbeat() -> FakeMessage:
    return FakeMessage("HEARTBEAT")


def sys_status(voltage=12400, current=183, remaining=76) -> FakeMessage:
    """SYS_STATUS as MAVLink sends it: millivolts, centiamps, percent."""
    return FakeMessage(
        "SYS_STATUS", voltage_battery=voltage, current_battery=current, battery_remaining=remaining
    )


def attitude(roll=0.0, pitch=0.0, yaw=0.0) -> FakeMessage:
    """ATTITUDE as MAVLink sends it: radians."""
    return FakeMessage("ATTITUDE", roll=roll, pitch=pitch, yaw=yaw)


class FakeLink:
    """Stands in for a pymavlink connection: `recv_match` blocks, as the real
    one does, until a message is queued or its timeout runs out."""

    def __init__(self, *, speaks: bool = True) -> None:
        self.inbox: queue.Queue = queue.Queue()
        self.speaks = speaks
        self.fail: Exception | None = None
        self.closed = False
        self.reading = threading.Event()  # set while a recv_match is under way
        self.closed_mid_read = False

    def send(self, *messages) -> None:
        for message in messages:
            self.inbox.put(message)

    def wait_heartbeat(self, timeout=None):
        return heartbeat() if self.speaks else None

    def recv_match(self, type=None, blocking=False, timeout=None):
        self.reading.set()
        try:
            if self.fail is not None:
                raise self.fail
            try:
                return self.inbox.get(timeout=timeout)
            except queue.Empty:
                return None
        finally:
            self.reading.clear()

    def close(self) -> None:
        self.closed_mid_read = self.reading.is_set()
        self.closed = True


class Factory:
    """Hands out links in order, or raises what it is given; records opens."""

    def __init__(self, *links) -> None:
        self.links = list(links)
        self.opened: list[FakeLink] = []

    def __call__(self, device, baud=None):
        item = self.links.pop(0) if self.links else FakeLink(speaks=False)
        if isinstance(item, Exception):
            raise item
        self.opened.append(item)
        return item


def make_stream(factory: Factory, **overrides) -> FlightControllerStream:
    fields = dict(
        device="/dev/serial0",
        link_factory=factory,
        stale_after=STALE,
        recv_timeout=RECV,
        backoff_min=0.01,
        backoff_max=0.04,
    )
    fields.update(overrides)
    return FlightControllerStream(**fields)


async def settle(condition, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.002)


def run(scenario, factory: Factory, **overrides) -> FlightControllerStream:
    stream = make_stream(factory, **overrides)

    async def main() -> None:
        await stream.start()
        try:
            await scenario(stream)
        finally:
            await stream.stop()

    asyncio.run(main())
    return stream


def connected(stream) -> bool:
    return stream.reading().component.connected


# -- readings ------------------------------------------------------------------


def test_battery_is_converted_out_of_the_units_mavlink_uses() -> None:
    link = FakeLink()

    async def scenario(stream):
        link.send(sys_status(voltage=12400, current=183, remaining=76))
        await settle(lambda: stream.reading().battery["voltage_v"] is not None)
        assert stream.reading().battery == {
            "voltage_v": 12.4,
            "current_a": 1.83,
            "remaining_percent": 76,
        }

    run(scenario, Factory(link))


def test_attitude_is_converted_from_radians_to_degrees() -> None:
    link = FakeLink()

    async def scenario(stream):
        link.send(attitude(roll=0.0175, pitch=-0.0349, yaw=3.1416))
        await settle(lambda: stream.reading().attitude["roll_deg"] is not None)
        assert stream.reading().attitude == {"roll_deg": 1.0, "pitch_deg": -2.0, "yaw_deg": 180.0}

    run(scenario, Factory(link))


def test_the_unknown_sentinels_become_null_rather_than_a_65_volt_battery() -> None:
    link = FakeLink()

    async def scenario(stream):
        link.send(sys_status(voltage=65535, current=-1, remaining=-1), attitude(roll=0.0175))
        await settle(lambda: stream.reading().attitude["roll_deg"] is not None)
        assert stream.reading().battery == {
            "voltage_v": None,
            "current_a": None,
            "remaining_percent": None,
        }

    run(scenario, Factory(link))


def test_attitude_is_live_not_sampled() -> None:
    # The old probe read one ATTITUDE per 2s window; now each one lands.
    link = FakeLink()

    async def scenario(stream):
        for roll in (0.0175, 0.0349, 0.0524):
            link.send(attitude(roll=roll))
            expected = round(roll * 180 / 3.14159265, 1)
            await settle(lambda: stream.reading().attitude["roll_deg"] == expected)

    run(scenario, Factory(link))


def test_the_battery_stays_while_other_messages_flow() -> None:
    # SYS_STATUS streams slower than ATTITUDE; it must not blank in between.
    link = FakeLink()

    async def scenario(stream):
        link.send(sys_status(voltage=12400))
        await settle(lambda: stream.reading().battery["voltage_v"] == 12.4)
        link.send(heartbeat(), attitude(roll=0.0175), attitude(roll=0.0349))
        await settle(lambda: stream.reading().attitude["roll_deg"] == 2.0)
        assert stream.reading().battery["voltage_v"] == 12.4

    run(scenario, Factory(link))


def test_a_frame_that_does_not_convert_costs_the_reading_not_the_link() -> None:
    link = FakeLink()

    async def scenario(stream):
        link.send(FakeMessage("SYS_STATUS"), attitude(roll=0.0175))  # no battery fields
        await settle(lambda: stream.reading().attitude["roll_deg"] == 1.0)
        assert connected(stream)
        assert stream.reading().battery["voltage_v"] is None

    run(scenario, Factory(link))


# -- losing the link and getting it back ---------------------------------------


def test_a_port_that_opens_but_never_speaks_is_not_connected() -> None:
    factory = Factory(FakeLink(speaks=False), FakeLink(speaks=False))

    async def scenario(stream):
        await settle(lambda: len(factory.opened) >= 2)
        assert not connected(stream)
        assert "No heartbeat within" in stream.reading().component.detail

    run(scenario, factory)
    assert all(link.closed for link in factory.opened)


def test_a_link_gone_quiet_is_dropped_and_reopened() -> None:
    # A UART does not error when the FC goes away; it just stops talking.
    first, second = FakeLink(), FakeLink()

    async def scenario(stream):
        first.send(sys_status(voltage=12400))
        await settle(lambda: stream.reading().battery["voltage_v"] == 12.4)
        # ... and then nothing, not even heartbeats.
        await settle(lambda: not connected(stream))
        assert "No heartbeat for" in stream.reading().component.detail
        # A value from a link that has since died would read as current.
        assert stream.reading().battery["voltage_v"] is None
        assert first.closed
        await settle(lambda: connected(stream))
        second.send(sys_status(voltage=11900))
        await settle(lambda: stream.reading().battery["voltage_v"] == 11.9)

    run(scenario, Factory(first, second))


def test_heartbeats_keep_the_link_alive() -> None:
    link = FakeLink()

    async def scenario(stream):
        for _ in range(8):
            link.send(heartbeat())
            await asyncio.sleep(STALE / 3)
        assert connected(stream)

    factory = Factory(link)
    run(scenario, factory)
    assert len(factory.opened) == 1


def test_a_pulled_cable_reconnects() -> None:
    # USB serial raises when the device goes away.
    first, second = FakeLink(), FakeLink()

    async def scenario(stream):
        await settle(lambda: connected(stream))
        first.fail = OSError("device reports readiness to read but returned no data")
        await settle(lambda: not connected(stream))
        assert "returned no data" in stream.reading().component.detail
        await settle(lambda: connected(stream) and len(factory.opened) == 2)

    factory = Factory(first, second)
    run(scenario, factory)
    assert first.closed


def test_a_port_that_will_not_open_is_retried_with_backoff() -> None:
    factory = Factory(*[OSError(f"No such file, attempt {i}") for i in range(6)], FakeLink())
    details: list[str] = []

    async def scenario(stream):
        async with asyncio.timeout(2):
            while not connected(stream):
                detail = stream.reading().component.detail
                if not details or details[-1] != detail:
                    details.append(detail)
                await asyncio.sleep(0.001)

    run(scenario, factory)
    retries = [d.split("retrying in ")[1] for d in details if "retrying in" in d]
    # Doubling from 0.01s, capped at 0.04s.
    assert retries[:4] == ["0.01s", "0.02s", "0.04s", "0.04s"]


def test_backoff_starts_over_after_a_link_that_worked() -> None:
    factory = Factory(OSError("gone"), OSError("gone"), FakeLink(), OSError("gone"), FakeLink())
    details: list[str] = []

    async def scenario(stream):
        async with asyncio.timeout(3):
            while len(factory.opened) < 2 or not connected(stream):
                detail = stream.reading().component.detail
                if not details or details[-1] != detail:
                    details.append(detail)
                await asyncio.sleep(0.001)

    run(scenario, factory)
    retries = [d.split("retrying in ")[1] for d in details if "retrying in" in d]
    # 0.01, 0.02, then the first link lives and goes quiet: 0.01 again.
    assert retries[:3] == ["0.01s", "0.02s", "0.01s"]


# -- start and stop --------------------------------------------------------------


def test_stop_waits_for_the_read_in_flight_before_closing() -> None:
    link = FakeLink()

    async def scenario(stream):
        await settle(lambda: connected(stream))
        await asyncio.to_thread(link.reading.wait, 1)

    run(scenario, Factory(link), recv_timeout=0.2)
    assert link.closed
    assert link.closed_mid_read is False


def test_stop_is_prompt() -> None:
    link = FakeLink()

    async def scenario(stream):
        await settle(lambda: connected(stream))
        started = time.monotonic()
        await stream.stop()
        assert time.monotonic() - started < 0.5

    run(scenario, Factory(link), recv_timeout=0.1)


def test_an_unconfigured_stream_opens_nothing() -> None:
    factory = Factory()

    async def scenario(stream):
        await asyncio.sleep(0.05)
        assert stream.reading().component.configured is False
        assert "not configured" in stream.reading().component.detail

    run(scenario, factory, device=None)
    assert factory.opened == []


def test_the_service_snapshot_shows_the_stream() -> None:
    link = FakeLink()
    stream = make_stream(Factory(link))
    service = VehicleStatusService(motor_device=None, flight_controller=stream)

    async def main() -> None:
        await service.start_streams()
        try:
            link.send(sys_status(voltage=12400), attitude(roll=0.0175))
            await settle(lambda: service.snapshot()["attitude"]["roll_deg"] == 1.0)
            snapshot = service.snapshot()
            assert snapshot["battery"]["voltage_v"] == 12.4
            assert snapshot["components"]["flight_controller"]["connected"] is True
            assert snapshot["fc_device"] == "/dev/serial0"
        finally:
            await service.stop_streams()

    asyncio.run(main())
    assert link.closed
