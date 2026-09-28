from __future__ import annotations

import logging
from dataclasses import asdict
from time import sleep
from threading import Event, Lock, Thread
from typing import Any, Callable

from serial import SerialException

from lib.ddsm115 import DDS115
from apps.api.services.components import ComponentSnapshot, utc_now
from apps.api.services.flight_controller import FlightControllerStream
from apps.api.services.pi_health import PI_HEALTH_UNAVAILABLE, PiHealthReader
from apps.vehicle_control.vehicle_controller import LOOP_INTERVAL, MAX_RPM, VehicleController
from settings import DEVICE, FC_BAUDRATE, FC_DEVICE, LEFT_SIDE, RIGHT_SIDE

logger = logging.getLogger(__name__)

DRIVER_HOLDS_BUS = "Held open by a driver on the status page"


class VehicleStatusService:
    def __init__(
        self,
        *,
        motor_device: str | None = DEVICE,
        fc_device: str | None = FC_DEVICE,
        fc_baudrate: int = FC_BAUDRATE,
        probe_interval_seconds: float = 2.0,
        motor_factory: Callable[..., DDS115] = DDS115,
        flight_controller: FlightControllerStream | None = None,
        sleep_func: Callable[[float], None] = sleep,
        pi_health_reader: Callable[[], dict[str, Any]] | None = None,
    ):
        self.motor_device = motor_device
        self.probe_interval_seconds = probe_interval_seconds
        self._motor_factory = motor_factory
        # Read continuously on the event loop, not by the probe thread; see
        # `start_streams`. The probe keeps the motor bus and the Pi.
        self.flight_controller = flight_controller or FlightControllerStream(
            device=fc_device, baudrate=fc_baudrate
        )
        self._sleep = sleep_func
        self._read_pi_health = pi_health_reader or PiHealthReader()
        self._stop_event = Event()
        self._lock = Lock()
        self._motor_bus_lock = Lock()
        # Held around every command to the driver's open port, so closing it
        # waits for a command already on the bus instead of cutting it off.
        self._drive_lock = Lock()
        self._drive_motor: DDS115 | None = None
        self._drive_owner: object | None = None
        self._thread: Thread | None = None
        self._current_command_rpm = 0
        self._components = {
            "motor_bus": ComponentSnapshot(
                configured=bool(self.motor_device),
                connected=False,
                detail="Probe has not run yet",
                checked_at=utc_now(),
            ),
        }
        self._motor_feedback = [
            {"motor_id": motor_id, "rpm": None, "current_raw": None}
            for motor_id in LEFT_SIDE + RIGHT_SIDE
        ]
        self._pi = dict(PI_HEALTH_UNAVAILABLE)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = Thread(target=self._probe_loop, daemon=True)
        self._thread.start()

    async def start_streams(self) -> None:
        """Start what runs on the event loop: the flight controller stream."""
        await self.flight_controller.start()

    async def stop_streams(self) -> None:
        await self.flight_controller.stop()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.probe_interval_seconds + 1.0)
            self._thread = None
        # A driver's session closes its own port; this is the backstop for a
        # shutdown that did not let it.
        self.close_drive()

    def snapshot(self) -> dict[str, Any]:
        fc = self.flight_controller.reading()
        with self._lock:
            components = {name: asdict(component) for name, component in self._components.items()}
            motor_feedback = list(self._motor_feedback)
            pi = dict(self._pi)
        components["flight_controller"] = asdict(fc.component)

        return {
            "service": "mt0d12-vehicle-api",
            "overall_status": self._overall_status(components),
            "timestamp": utc_now(),
            "motor_device": self.motor_device,
            "fc_device": self.flight_controller.device,
            "motor_ids": {
                "left": list(LEFT_SIDE),
                "right": list(RIGHT_SIDE),
            },
            "components": components,
            "battery": dict(fc.battery),
            "attitude": dict(fc.attitude),
            "pi": pi,
            "motor_feedback": motor_feedback,
        }

    def start_motors(self, rpm: int) -> dict[str, Any]:
        self._validate_rpm(rpm)
        self._run_motor_ramp(target_rpm=rpm)
        return self._motor_command_response(
            action="start",
            target_rpm=rpm,
            detail=f"All motors ramped to {rpm} rpm",
        )

    def stop_motors(self) -> dict[str, Any]:
        self._run_motor_ramp(target_rpm=0)
        return self._motor_command_response(
            action="stop",
            target_rpm=0,
            detail="All motors ramped down to 0 rpm",
        )

    # -- Driving from /ws/status ----------------------------------------------
    # Blocking, like everything else on the motor bus: the WebSocket route runs
    # these in a worker thread.

    def open_drive(self, owner: object) -> None:
        """
        Open the motor bus for `owner` and hold it until `close_drive`.

        /motors/start opens the port per request and ramps; a driver sends a
        command every 50 ms, so the port stays open for the whole session. One
        driver at a time: a second one, or a ramp, is refused while it lasts.
        The owner is what `drive` and `close_drive` check, so a session that
        has ended can never command, or close, the bus of the one after it.
        """
        if not self.motor_device:
            self._set_motor_component(connected=False, detail="DEVICE is not configured")
            raise RuntimeError("Motor device is not configured")

        with self._motor_bus_lock:
            if self._drive_motor is not None:
                raise RuntimeError("Another viewer is driving")
            try:
                self._drive_motor = self._motor_factory(device=self.motor_device)
                self._drive_owner = owner
            except (RuntimeError, SerialException, ValueError) as exc:
                self._set_motor_component(connected=False, detail=str(exc))
                raise RuntimeError(str(exc)) from exc

        self._set_motor_component(connected=True, detail=DRIVER_HOLDS_BUS)

    def drive(self, owner: object, left_rpm: int, right_rpm: int) -> None:
        """
        One command to each side. A no-op unless `owner` holds the bus: a
        command that was waiting while the session closed is stale.
        """
        with self._drive_lock:
            if self._drive_motor is None or self._drive_owner is not owner:
                return
            try:
                self._send_side_rpms(self._drive_motor, left_rpm, right_rpm)
            except (SerialException, OSError) as exc:
                raise RuntimeError(str(exc)) from exc

    def close_drive(self, owner: object | None = None) -> None:
        """
        Stop the motors and release the port, if `owner` holds it - or
        whoever does, with no owner. Safe to call when no one is driving, and
        more than once.
        """
        with self._motor_bus_lock, self._drive_lock:
            if self._drive_motor is None:
                return
            if owner is not None and owner is not self._drive_owner:
                return
            motor, self._drive_motor, self._drive_owner = self._drive_motor, None, None
            try:
                self._send_side_rpms(motor, 0, 0)
            finally:
                motor.close()
                with self._lock:
                    self._current_command_rpm = 0

    def _probe_loop(self) -> None:
        while not self._stop_event.is_set():
            self._probe_once()
            self._stop_event.wait(self.probe_interval_seconds)

    def _probe_once(self) -> None:
        """One pass over the hardware. Separate from the loop so it can be run
        a single time - by a test, or by anything that wants a fresh reading
        without waiting for the next tick."""
        motor_bus = self._probe_motor_bus()
        pi = self._probe_pi()

        with self._lock:
            self._pi = pi
            self._components["motor_bus"] = motor_bus

    def _probe_pi(self) -> dict[str, Any]:
        """The board's own health. Never costs the rest of the probe: a reader
        that fails outright reports nothing known, like an absent FC."""
        try:
            return self._read_pi_health()
        except Exception as error:
            logger.warning("Pi health read failed", exc_info=error)
            return dict(PI_HEALTH_UNAVAILABLE)

    def _probe_motor_bus(self) -> ComponentSnapshot:
        checked_at = utc_now()
        if not self.motor_device:
            return ComponentSnapshot(
                configured=False,
                connected=False,
                detail="DEVICE is not configured",
                checked_at=checked_at,
            )

        with self._motor_bus_lock:
            if self._drive_motor is not None:
                # Commands are reaching the motors, which is all this probe
                # would find out by opening the port a second time.
                return ComponentSnapshot(
                    configured=True,
                    connected=True,
                    detail=DRIVER_HOLDS_BUS,
                    checked_at=checked_at,
                )
            motor = None
            try:
                motor = self._motor_factory(device=self.motor_device)
            except (RuntimeError, SerialException, ValueError) as exc:
                return ComponentSnapshot(
                    configured=True,
                    connected=False,
                    detail=str(exc),
                    checked_at=checked_at,
                )
            finally:
                if motor is not None:
                    motor.close()

        return ComponentSnapshot(
            configured=True,
            connected=True,
            detail="Serial device opened successfully; live motor telemetry is not wired yet",
            checked_at=checked_at,
        )

    @staticmethod
    def _overall_status(components: dict[str, dict[str, Any]]) -> str:
        if all(
            component["configured"] and component["connected"] for component in components.values()
        ):
            return "ok"
        return "degraded"

    def _run_motor_ramp(self, *, target_rpm: int) -> None:
        if not self.motor_device:
            self._set_motor_component(connected=False, detail="DEVICE is not configured")
            raise RuntimeError("Motor device is not configured")

        with self._motor_bus_lock:
            if self._drive_motor is not None:
                raise RuntimeError("Motors are being driven from the status page")
            motor = None
            try:
                motor = self._motor_factory(device=self.motor_device)
            except (RuntimeError, SerialException, ValueError) as exc:
                self._set_motor_component(connected=False, detail=str(exc))
                raise RuntimeError(str(exc)) from exc

            try:
                current_rpm = self._current_rpm()
                while current_rpm != target_rpm:
                    current_rpm = int(VehicleController._ramp_toward(current_rpm, target_rpm))
                    self._send_motor_commands(motor, current_rpm)
                    if current_rpm != target_rpm:
                        self._sleep(LOOP_INTERVAL)
            finally:
                motor.close()

        self._set_motor_component(
            connected=True,
            detail=f"Serial device opened successfully; last commanded base rpm is {target_rpm}",
        )

    def _send_motor_commands(self, motor: DDS115, base_rpm: int) -> None:
        self._send_side_rpms(motor, base_rpm, base_rpm)
        with self._lock:
            self._current_command_rpm = base_rpm

    def _send_side_rpms(self, motor: DDS115, left_rpm: int, right_rpm: int) -> None:
        """Right-side motors are negated to match their mounting direction."""
        for motor_id in LEFT_SIDE:
            motor.send_rpm(motor_id, rpm=left_rpm)
        for motor_id in RIGHT_SIDE:
            motor.send_rpm(motor_id, rpm=right_rpm * (-1))

        with self._lock:
            self._motor_feedback = [
                {
                    "motor_id": motor_id,
                    "rpm": left_rpm if motor_id in LEFT_SIDE else right_rpm * (-1),
                    "current_raw": None,
                }
                for motor_id in LEFT_SIDE + RIGHT_SIDE
            ]

    def _motor_command_response(
        self, *, action: str, target_rpm: int, detail: str
    ) -> dict[str, Any]:
        return {
            "service": "mt0d12-vehicle-api",
            "action": action,
            "target_rpm": target_rpm,
            "current_rpm": self._current_rpm(),
            "detail": detail,
            "timestamp": utc_now(),
        }

    def _current_rpm(self) -> int:
        with self._lock:
            return self._current_command_rpm

    def _set_motor_component(self, *, connected: bool, detail: str) -> None:
        with self._lock:
            self._components["motor_bus"] = ComponentSnapshot(
                configured=bool(self.motor_device),
                connected=connected,
                detail=detail,
                checked_at=utc_now(),
            )

    @staticmethod
    def _validate_rpm(rpm: int) -> None:
        if not -MAX_RPM <= rpm <= MAX_RPM:
            raise ValueError(f"rpm must be between {-MAX_RPM} and {MAX_RPM}")
