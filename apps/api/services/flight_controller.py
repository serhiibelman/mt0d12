"""The flight controller, read as a stream over one link that stays open.

The status probe used to reopen the MAVLink link every 2s and read for 0.6s,
so attitude was up to 2s old and every probe paid for a heartbeat wait. Here
the link opens once and every `ATTITUDE`, `SYS_STATUS` and `HEARTBEAT` is taken
as it arrives, so the snapshot holds the latest reading - the 5 Hz status
stream shows attitude at most 200 ms old.

pymavlink blocks, so each call runs in a worker thread (`to_thread`), short
enough (`recv_timeout`) that shutdown never waits long. The link counts as
lost when a call fails - a USB cable pulled out raises - or when no heartbeat
has arrived for `stale_after` - a UART just goes quiet. Either way it is closed
and reopened with backoff, 0.5s doubling to 10s, so an unplugged FC costs a
retry every 10s rather than a busy loop.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from math import degrees
from typing import Any, Callable

from pymavlink import mavutil

from apps.api.services.components import ComponentSnapshot, utc_now
from settings import FC_BAUDRATE, FC_DEVICE

logger = logging.getLogger(__name__)

# How long a freshly opened link may take to say its first heartbeat.
HEARTBEAT_TIMEOUT_SECONDS = 1.0
# The FC sends a heartbeat every second; three missed is a link gone quiet.
HEARTBEAT_STALE_SECONDS = 3.0
# One blocking read; also the longest shutdown waits for a thread to finish.
RECV_TIMEOUT_SECONDS = 0.25
BACKOFF_MIN_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 10.0

MESSAGE_TYPES = ["HEARTBEAT", "SYS_STATUS", "ATTITUDE"]

# SYS_STATUS reports "no reading" in-band rather than as null, and the
# sentinels are values that look plausible: 65535 mV reads as a 65 V battery
# unless it is caught here.
VOLTAGE_UNKNOWN = 65535  # uint16 max, millivolts
CURRENT_UNKNOWN = -1  # centiamps
REMAINING_UNKNOWN = -1  # percent

BATTERY_UNAVAILABLE = {"voltage_v": None, "current_a": None, "remaining_percent": None}
ATTITUDE_UNAVAILABLE = {"roll_deg": None, "pitch_deg": None, "yaw_deg": None}


def battery_from(message: Any) -> dict[str, Any]:
    """Volts, amps and percent out of SYS_STATUS, which reports mV, cA and %."""
    voltage = message.voltage_battery
    current = message.current_battery
    remaining = message.battery_remaining
    return {
        "voltage_v": None if voltage == VOLTAGE_UNKNOWN else round(voltage / 1000, 1),
        "current_a": None if current == CURRENT_UNKNOWN else round(current / 100, 2),
        "remaining_percent": None if remaining == REMAINING_UNKNOWN else int(remaining),
    }


def attitude_from(message: Any) -> dict[str, Any]:
    """Degrees out of ATTITUDE, which reports radians.

    The units are in the field names on purpose: radians arriving somewhere
    that expects degrees is the classic way this goes wrong quietly.
    """
    return {
        "roll_deg": round(degrees(message.roll), 1),
        "pitch_deg": round(degrees(message.pitch), 1),
        "yaw_deg": round(degrees(message.yaw), 1),
    }


@dataclass(frozen=True)
class FcReading:
    """Everything the snapshot shows about the FC, replaced whole on every
    change, so no reader ever sees half an update."""

    component: ComponentSnapshot
    battery: dict[str, Any]
    attitude: dict[str, Any]


class LinkLost(Exception):
    """The link failed or went quiet; the message says how."""


class FlightControllerStream:
    def __init__(
        self,
        *,
        device: str | None = FC_DEVICE,
        baudrate: int = FC_BAUDRATE,
        link_factory: Callable[..., Any] = mavutil.mavlink_connection,
        heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS,
        stale_after: float = HEARTBEAT_STALE_SECONDS,
        recv_timeout: float = RECV_TIMEOUT_SECONDS,
        backoff_min: float = BACKOFF_MIN_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
    ) -> None:
        self.device = device
        self.baudrate = baudrate
        self._link_factory = link_factory
        self._heartbeat_timeout = heartbeat_timeout
        self._stale_after = stale_after
        self._recv_timeout = recv_timeout
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._task: asyncio.Task[None] | None = None
        # The worker-thread call in flight, if any; see `_blocking`.
        self._in_flight: asyncio.Future[Any] | None = None
        detail = "Not started yet" if device else "FC_DEVICE is not configured"
        self._reading = self._down(detail)

    def reading(self) -> FcReading:
        return self._reading

    async def run(self) -> None:
        """Stream until cancelled, closing the link on the way out. For the
        rover's supervisor; `start` and `stop` run the same as a task."""
        if self.device:
            await self._run()

    async def start(self) -> None:
        if self.device and self._task is None:
            self._task = asyncio.create_task(self._run(), name="flight-controller")

    async def stop(self) -> None:
        """Cancel the stream and wait until the link is closed."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                await self._stream()
            except LinkLost as lost:
                reason = str(lost)
            if self._reading.component.connected:
                failures = 0  # a link that worked starts the backoff over
            delay = min(self._backoff_max, self._backoff_min * 2**failures)
            failures += 1
            logger.info("Flight controller link lost (%s); retrying in %gs", reason, delay)
            self._reading = self._down(f"{reason}; retrying in {delay:g}s")
            await asyncio.sleep(delay)

    async def _stream(self) -> None:
        """Open the link and read it until it is lost. Always closes it."""
        link = await self._blocking(self._link_factory, self.device, baud=self.baudrate)
        try:
            heartbeat = await self._blocking(link.wait_heartbeat, timeout=self._heartbeat_timeout)
            if heartbeat is None:
                # wait_heartbeat returns None on timeout rather than raising,
                # so a port that opens but never speaks is caught here.
                raise LinkLost(f"No heartbeat within {self._heartbeat_timeout:g}s")
            self._on_heartbeat(heartbeat)
            loop = asyncio.get_running_loop()
            last_heartbeat = loop.time()
            while True:
                message = await self._blocking(
                    link.recv_match,
                    type=MESSAGE_TYPES,
                    blocking=True,
                    timeout=self._recv_timeout,
                )
                if message is not None and message.get_type() == "HEARTBEAT":
                    last_heartbeat = loop.time()
                    self._on_heartbeat(message)
                elif message is not None:
                    self._on_telemetry(message)
                if loop.time() - last_heartbeat > self._stale_after:
                    raise LinkLost(f"No heartbeat for {self._stale_after:g}s")
        finally:
            await self._close(link)

    async def _blocking(self, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """`call` in a worker thread; any failure of it is a lost link.

        Shielded and kept, because a thread cannot be cancelled: when the
        stream is stopped mid-read the read carries on, and `_close` must wait
        for it before closing the link under it.
        """
        self._in_flight = asyncio.ensure_future(asyncio.to_thread(call, *args, **kwargs))
        try:
            return await asyncio.shield(self._in_flight)
        except Exception as exc:
            raise LinkLost(str(exc) or type(exc).__name__) from exc

    async def _close(self, link: Any) -> None:
        in_flight = self._in_flight
        if in_flight is not None and not in_flight.done():
            await asyncio.wait([in_flight])
        if in_flight is not None and not in_flight.cancelled():
            in_flight.exception()  # retrieved: a read that failed on the way out is expected
        with contextlib.suppress(Exception):
            await asyncio.to_thread(link.close)

    def _on_heartbeat(self, heartbeat: Any) -> None:
        system_id = getattr(heartbeat, "get_srcSystem", lambda: None)()
        self._reading = FcReading(
            component=ComponentSnapshot(
                configured=True,
                connected=True,
                detail=f"Heartbeat received from system {system_id}",
                checked_at=utc_now(),
            ),
            battery=self._reading.battery,
            attitude=self._reading.attitude,
        )

    def _on_telemetry(self, message: Any) -> None:
        # A frame that does not convert costs that reading, not the link.
        try:
            kind = message.get_type()
            if kind == "SYS_STATUS":
                reading = FcReading(
                    self._reading.component, battery_from(message), self._reading.attitude
                )
            elif kind == "ATTITUDE":
                reading = FcReading(
                    self._reading.component, self._reading.battery, attitude_from(message)
                )
            else:
                return
        except Exception as error:
            logger.debug("Flight controller message could not be read", exc_info=error)
            return
        self._reading = reading

    def _down(self, detail: str) -> FcReading:
        # A reading from a link that has since dropped would read as current.
        # Nothing known beats quietly wrong.
        return FcReading(
            component=ComponentSnapshot(
                configured=bool(self.device),
                connected=False,
                detail=detail,
                checked_at=utc_now(),
            ),
            battery=dict(BATTERY_UNAVAILABLE),
            attitude=dict(ATTITUDE_UNAVAILABLE),
        )
