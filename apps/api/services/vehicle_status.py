"""The vehicle's state in one snapshot, and the /motors ramps.

Everything here runs on the event loop and is only touched from it: the probe
is a task, the routes that read the snapshot are `async def`, and the telemetry
sampler and status broadcaster are coroutines. So the state needs no lock.

What goes into the snapshot comes from three places. The motor bus reports
itself (`MotorBus`) and the flight controller streams (`FlightControllerStream`);
the probe task only reads the Pi's own health, every `probe_interval_seconds`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from apps.api.services.components import utc_now
from apps.api.services.flight_controller import FlightControllerStream
from apps.api.services.pi_health import PI_HEALTH_UNAVAILABLE, PiHealthReader
from apps.vehicle_control.vehicle_controller import LOOP_INTERVAL, MAX_RPM, VehicleController
from lib.ddsm115 import MotorBus
from settings import LEFT_SIDE, RIGHT_SIDE

logger = logging.getLogger(__name__)

RAMP = "A /motors ramp"


class VehicleStatusService:
    def __init__(
        self,
        *,
        bus: MotorBus,
        flight_controller: FlightControllerStream,
        probe_interval_seconds: float = 2.0,
        ramp_interval: float = LOOP_INTERVAL,
        pi_health_reader: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ):
        self.bus = bus
        self.flight_controller = flight_controller
        self.probe_interval_seconds = probe_interval_seconds
        self._ramp_interval = ramp_interval
        self._read_pi_health = pi_health_reader or PiHealthReader()
        self._pi = dict(PI_HEALTH_UNAVAILABLE)

    async def run(self) -> None:
        """Probe until cancelled. For the rover's supervisor."""
        while True:
            await self.probe_once()
            await asyncio.sleep(self.probe_interval_seconds)

    async def probe_once(self) -> None:
        """One reading of the Pi. Separate from the loop so it can be run a
        single time - by a test, or by anything that wants a fresh reading
        without waiting for the next tick. Never raises: a reader that fails
        outright reports nothing known, like an absent FC."""
        try:
            self._pi = await self._read_pi_health()
        except Exception as error:
            logger.warning("Pi health read failed", exc_info=error)
            self._pi = dict(PI_HEALTH_UNAVAILABLE)

    def snapshot(self) -> dict[str, Any]:
        fc = self.flight_controller.reading()
        components = {
            "motor_bus": self.bus.component(),
            "flight_controller": asdict(fc.component),
        }
        return {
            "service": "mt0d12-vehicle-api",
            "overall_status": self._overall_status(components),
            "timestamp": utc_now(),
            "motor_device": self.bus.device,
            "fc_device": self.flight_controller.device,
            "motor_ids": {
                "left": list(LEFT_SIDE),
                "right": list(RIGHT_SIDE),
            },
            "components": components,
            "battery": dict(fc.battery),
            "attitude": dict(fc.attitude),
            "pi": dict(self._pi),
            "motor_feedback": self.bus.feedback(),
        }

    async def start_motors(self, rpm: int) -> dict[str, Any]:
        self._validate_rpm(rpm)
        await self._run_motor_ramp(target_rpm=rpm)
        return self._motor_command_response(
            action="start",
            target_rpm=rpm,
            detail=f"All motors ramped to {rpm} rpm",
        )

    async def stop_motors(self) -> dict[str, Any]:
        await self._run_motor_ramp(target_rpm=0)
        return self._motor_command_response(
            action="stop",
            target_rpm=0,
            detail="All motors ramped down to 0 rpm",
        )

    async def _run_motor_ramp(self, *, target_rpm: int) -> None:
        """Ramp every motor to `target_rpm` and leave them there.

        Holds the bus only while ramping, so the motors keep turning after
        with nobody holding it - what /motors/start is for. Starts from what
        was last sent, so a stop after a start ramps down rather than jumping.
        A driver who takes the bus meanwhile starts from zero, as ever.
        """
        ramp = object()
        self.bus.claim(ramp, RAMP)
        try:
            left, right = self.bus.commanded_sides()
            current = round((left + right) / 2)
            while current != target_rpm:
                current = int(VehicleController._ramp_toward(current, target_rpm))
                await self.bus.drive(ramp, current, current)
                if current != target_rpm:
                    await asyncio.sleep(self._ramp_interval)
        finally:
            await self.bus.release(ramp, stop=False)

    @staticmethod
    def _overall_status(components: dict[str, dict[str, Any]]) -> str:
        if all(
            component["configured"] and component["connected"] for component in components.values()
        ):
            return "ok"
        return "degraded"

    def _motor_command_response(
        self, *, action: str, target_rpm: int, detail: str
    ) -> dict[str, Any]:
        left, right = self.bus.commanded_sides()
        return {
            "service": "mt0d12-vehicle-api",
            "action": action,
            "target_rpm": target_rpm,
            "current_rpm": round((left + right) / 2),
            "detail": detail,
            "timestamp": utc_now(),
        }

    @staticmethod
    def _validate_rpm(rpm: int) -> None:
        if not -MAX_RPM <= rpm <= MAX_RPM:
            raise ValueError(f"rpm must be between {-MAX_RPM} and {MAX_RPM}")
