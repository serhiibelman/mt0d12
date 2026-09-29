"""
The motor bus, owned by one object for the life of the process.

The API and `vehicle_control` used to open the serial port each on their own,
and whichever started second got a 503. Here the port opens once and stays
open, and everything that moves the rover - the status page, the gamepad, a
/motors ramp - asks this object for it. One of them holds it at a time:
`claim` it, command it, `release` it, which stops the motors.

Every call on the port runs on the bus's own worker thread, never the event
loop's shared pool. So the calls run one at a time and in the order they were
made - no lock around the port - and a drive command never queues behind a
flight controller read, a spool write or a camera open waiting for a free
worker. That is the priority: driving never waits for telemetry.

The link is watched the way the flight controller's is. A command that fails
- the USB cable pulled out - closes the port, and so does its device node
going away while nobody is driving. It is reopened with backoff, 0.5s
doubling to 10s.
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from serial import SerialException

from lib.ddsm115.ddsm115 import DDS115
from settings import DEVICE, LEFT_SIDE, RIGHT_SIDE

logger = logging.getLogger(__name__)

# How often an idle bus checks that its device is still there.
CHECK_INTERVAL_SECONDS = 2.0
BACKOFF_MIN_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 10.0

# What a port raises when the device behind it has gone.
PORT_ERRORS = (SerialException, OSError)


class BusUnavailable(RuntimeError):
    """The port is not open: not configured, not there, or shutting down."""


class BusBusy(RuntimeError):
    """Someone else holds the bus."""


class MotorBus:
    def __init__(
        self,
        *,
        device: str | None = DEVICE,
        motor_factory: Callable[..., DDS115] = DDS115,
        device_present: Callable[[str], bool] = os.path.exists,
        check_interval: float = CHECK_INTERVAL_SECONDS,
        backoff_min: float = BACKOFF_MIN_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
    ) -> None:
        self.device = device
        self._motor_factory = motor_factory
        self._device_present = device_present
        self._check_interval = check_interval
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        # One thread: the calls on the port are serialised by construction.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="motor-bus")
        self._motor: DDS115 | None = None
        self._holder: object | None = None
        self._holder_name: str | None = None
        self._halted = False
        self._lost = asyncio.Event()
        self._lost_reason = ""
        self._detail = "Not started yet" if device else "DEVICE is not configured"
        self._checked_at = utc_now()
        # Last commanded RPM per motor, as sent: right side negated.
        self._commanded: dict[int, int | None] = {m: None for m in LEFT_SIDE + RIGHT_SIDE}

    # -- state, for the snapshot ----------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._motor is not None

    @property
    def holder_name(self) -> str | None:
        return self._holder_name

    def component(self) -> dict[str, Any]:
        """The bus as a snapshot component."""
        detail = self._detail
        if self._motor is not None:
            detail = f"{self._holder_name} is driving" if self._holder else "Open, idle"
        return {
            "configured": bool(self.device),
            "connected": self._motor is not None,
            "detail": detail,
            "checked_at": self._checked_at,
        }

    def feedback(self) -> list[dict[str, Any]]:
        return [
            {"motor_id": motor_id, "rpm": rpm, "current_raw": None}
            for motor_id, rpm in self._commanded.items()
        ]

    def commanded_sides(self) -> tuple[int, int]:
        """The last (left, right) command, in the caller's sense - right not
        negated. Zero for a side nothing has been sent to."""
        left = self._commanded[LEFT_SIDE[0]] or 0
        right = -(self._commanded[RIGHT_SIDE[0]] or 0)
        return left, right

    # -- holding it -------------------------------------------------------------

    def claim(self, owner: object, name: str) -> None:
        """Hold the bus for `owner` until `release`. Refused while someone else
        holds it, or while the port is not open. `name` says who, in the
        refusal someone else gets: "The gamepad is driving"."""
        if self._halted:
            raise BusUnavailable("The rover is shutting down")
        if self._motor is None:
            raise BusUnavailable(f"Motor bus is not available: {self._detail}")
        if self._holder is not None and self._holder is not owner:
            raise BusBusy(f"{self._holder_name} is driving")
        self._holder, self._holder_name = owner, name

    async def release(self, owner: object, *, stop: bool = True) -> None:
        """Let go, stopping the motors first unless `stop` is False (a ramp
        that leaves them turning). A no-op unless `owner` holds the bus, so a
        session that has ended can never release the one after it. Never
        raises: a stop that fails is logged, as nothing more can be done."""
        if self._holder is not owner:
            return
        self._holder = self._holder_name = None
        if stop:
            await self._stop_quietly()

    async def halt(self) -> None:
        """Stop the motors, whoever holds them, and refuse every claim after.
        The first step of shutdown."""
        self._halted = True
        self._holder = self._holder_name = None
        await self._stop_quietly()

    # -- commands ---------------------------------------------------------------

    async def drive(self, owner: object, left_rpm: int, right_rpm: int) -> None:
        """One command to each side. A no-op unless `owner` holds the bus: a
        command that was waiting while its session ended is stale."""
        if self._holder is not owner:
            return
        await self._send_sides(left_rpm, right_rpm)

    async def brake(self, owner: object) -> None:
        if self._holder is not owner:
            return
        motor = self._require_open()
        await self._command(lambda: [motor.set_brake(m) for m in LEFT_SIDE + RIGHT_SIDE])
        for motor_id in self._commanded:
            self._commanded[motor_id] = 0

    # -- the link -----------------------------------------------------------------

    async def run(self) -> None:
        """Keep the port open until cancelled; stop the motors and close it
        on the way out. For the rover's supervisor."""
        if not self.device:
            return
        failures = 0
        try:
            while True:
                reason = await self._hold_open()
                delay = min(self._backoff_max, self._backoff_min * 2**failures)
                if self._motor is not None:
                    # A port that opened starts the backoff over.
                    failures, delay = 0, self._backoff_min
                    self._set_detail(reason)
                    await self._close()
                failures += 1
                logger.info("Motor bus unavailable (%s); retrying in %gs", reason, delay)
                self._set_detail(f"{reason}; retrying in {delay:g}s")
                await asyncio.sleep(delay)
        finally:
            if self._motor is not None:
                await self._stop_quietly()
                await self._close()

    async def _hold_open(self) -> str:
        """Open the port and watch it until it is lost; returns why."""
        try:
            motor = await self._call(self._motor_factory, device=self.device)
        except (RuntimeError, ValueError, *PORT_ERRORS) as exc:
            return str(exc) or type(exc).__name__
        self._motor = motor
        self._lost.clear()
        self._set_detail("Open")
        logger.info("Motor bus open on %s", self.device)
        while True:
            try:
                async with asyncio.timeout(self._check_interval):
                    await self._lost.wait()
                    return self._lost_reason
            except TimeoutError:
                # A USB adapter unplugged while idle raises nothing until the
                # next write; its device node disappearing is the sign.
                if self._holder is None and not self._device_present(self.device):
                    return f"{self.device} is gone"

    async def _close(self) -> None:
        motor, self._motor = self._motor, None
        if motor is None:
            return
        try:
            await self._call(motor.close)
        except Exception as error:
            logger.debug("Closing the motor bus failed", exc_info=error)

    # -- plumbing ---------------------------------------------------------------

    async def _send_sides(self, left_rpm: int, right_rpm: int) -> None:
        """Right-side motors are negated to match their mounting direction."""
        motor = self._require_open()
        commands = [(m, left_rpm) for m in LEFT_SIDE] + [(m, -right_rpm) for m in RIGHT_SIDE]

        def send() -> None:
            for motor_id, rpm in commands:
                motor.send_rpm(motor_id, rpm=rpm)

        await self._command(send)
        self._commanded.update(commands)

    async def _stop_quietly(self) -> None:
        if self._motor is None:
            return
        try:
            await self._send_sides(0, 0)
        except Exception:
            # The motors keep their last command, so this must at least be loud.
            logger.exception("Could not stop the motors")

    async def _command(self, call: Callable[[], Any]) -> None:
        """A call on the open port. One that fails means the port is gone."""
        try:
            await self._call(call)
        except PORT_ERRORS as exc:
            reason = str(exc) or type(exc).__name__
            self._lost_reason = f"Command failed: {reason}"
            self._lost.set()
            raise BusUnavailable(reason) from exc

    def _require_open(self) -> DDS115:
        if self._motor is None:
            raise BusUnavailable(f"Motor bus is not available: {self._detail}")
        return self._motor

    async def _call(self, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        `call` on the bus thread, in the order the calls were made.

        Shielded, because a call already handed to the thread must still
        happen: cancelling the future would drop it if it had not started, and
        a stop sent on the way out of a cancelled task is exactly that call.
        """
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, lambda: call(*args, **kwargs))
        return await asyncio.shield(future)

    def _set_detail(self, detail: str) -> None:
        self._detail = detail
        self._checked_at = utc_now()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
