"""Driving from the status page, one session per WebSocket connection.

The page arms, then sends stick positions 20 times a second; the session turns
the newest one into motor commands with the same ramp and mixing the gamepad
path uses (`VehicleController`), so a stick feels the same on either.

Commands and the motor bus run at different speeds: a command arrives every
50 ms, while a pass over four motors can take longer, since each `send_rpm`
waits for its reply. So the reader only records the newest stick position and
a separate task applies it when the bus is free - newest wins, the rule
`StatusBroadcaster` applies to updates - and the rover never works through a
backlog of positions the driver has already moved on from.

The bus calls block, so they run in a worker thread. A thread cannot be
cancelled; a cancelled session stops waiting for one, but the call itself
still finishes, which is what makes the stop on the way out reliable.
"""

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from apps.vehicle_control.vehicle_controller import DEAD_ZONE, MAX_RPM, VehicleController

logger = logging.getLogger(__name__)

# Tells the page what happened to its drive state. Status updates have no
# "type" field, which is how the page tells the two apart.
Notify = Callable[[dict[str, Any]], Awaitable[None]]


class MotorBus(Protocol):
    """The part of `VehicleStatusService` a session drives through."""

    def open_drive(self, owner: object) -> None: ...

    def drive(self, owner: object, left_rpm: int, right_rpm: int) -> None: ...

    def close_drive(self, owner: object | None = None) -> None: ...


def drive_state(armed: bool, detail: str) -> dict[str, Any]:
    return {"type": "drive", "armed": armed, "detail": detail}


def axis(value: Any) -> float | None:
    """A stick axis from the page, clamped to -1..1; None if it is not a number.

    JSON has no NaN, but `json.loads` accepts it, and a NaN would pass every
    comparison in the mixing unchanged.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return max(-1.0, min(1.0, float(value)))


class DriveSession:
    def __init__(self, bus: MotorBus, notify: Notify) -> None:
        self._bus = bus
        self._notify = notify
        # Arming and disarming each wait on the bus; the lock keeps one from
        # starting while the other is still under way.
        self._lock = asyncio.Lock()
        self._armed = False
        self._throttle = 0.0
        self._steer = 0.0
        self._current_rpm = 0.0
        self._changed = asyncio.Event()
        self._opening: asyncio.Future[None] | None = None

    @property
    def armed(self) -> bool:
        return self._armed

    async def arm(self) -> None:
        async with self._lock:
            if self._armed:
                return
            # Shielded, and kept, because a thread cannot be cancelled: if the
            # connection ends mid-open the port may still open after it, and
            # `close` has to wait for that before it can release it.
            self._opening = asyncio.ensure_future(asyncio.to_thread(self._bus.open_drive, self))
            try:
                await asyncio.shield(self._opening)
            except RuntimeError as exc:
                await self._notify(drive_state(False, str(exc)))
                return
            # A fresh start: nothing from before the last disarm carries over.
            self._throttle = self._steer = self._current_rpm = 0.0
            self._armed = True
        await self._notify(drive_state(True, "Driving"))

    async def disarm(self, detail: str) -> None:
        """Stop at once, rather than ramping, and release the bus.

        The same stop `VehicleController` makes when its link goes quiet: a
        ramp down from full speed would be seconds of driving blind.
        """
        async with self._lock:
            if not self._armed:
                return
            self._armed = False
            await self._close_bus()
        await self._notify(drive_state(False, detail))

    async def close(self) -> None:
        """Stop and release without telling the page, which may be gone. For
        the end of the connection, however it ended."""
        self._armed = False
        if self._opening is not None:
            with contextlib.suppress(Exception):
                await self._opening
        # Owner-checked, so a no-op unless this session holds the bus.
        await self._close_bus()

    def command(self, throttle: float, steer: float) -> None:
        """Record the newest stick position; `run_motors` applies it."""
        if not self._armed:
            return
        self._throttle = throttle
        self._steer = steer
        self._changed.set()

    async def run_motors(self) -> None:
        """Apply the newest command whenever there is one. Runs for the whole
        connection, armed or not."""
        while True:
            await self._changed.wait()
            self._changed.clear()
            if not self._armed:
                continue
            # Throttle forward is positive here; the gamepad's stick-up is -1.
            target = 0.0 if abs(self._throttle) < DEAD_ZONE else self._throttle * MAX_RPM
            self._current_rpm = VehicleController._ramp_toward(self._current_rpm, target)
            left, right = VehicleController._compute_side_rpms(self._current_rpm, self._steer)
            try:
                await asyncio.to_thread(self._bus.drive, self, round(left), round(right))
            except RuntimeError as exc:
                logger.warning("Drive command failed: %s", exc)
                await self.disarm(f"Motor bus failed: {exc}")

    async def _close_bus(self) -> None:
        try:
            await asyncio.to_thread(self._bus.close_drive, self)
        except Exception:
            # Nothing more can be done from here; the motors keep their last
            # command, so this must at least be loud.
            logger.exception("Could not stop the motors")
