from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import degrees
from time import monotonic, sleep
from threading import Event, Lock, Thread
from typing import Any, Callable

from pymavlink import mavutil
from serial import SerialException

from lib.ddsm115 import DDS115
from apps.api.services.pi_health import PI_HEALTH_UNAVAILABLE, PiHealthReader
from apps.vehicle_control.vehicle_controller import LOOP_INTERVAL, MAX_RPM, VehicleController
from settings import DEVICE, FC_BAUDRATE, FC_DEVICE, LEFT_SIDE, RIGHT_SIDE

logger = logging.getLogger(__name__)

FC_HEARTBEAT_TIMEOUT_SECONDS = 1.0

# SYS_STATUS reports "no reading" in-band rather than as null, and the
# sentinels are values that look plausible: 65535 mV reads as a 65 V battery
# unless it is caught here.
VOLTAGE_UNKNOWN = 65535  # uint16 max, millivolts
CURRENT_UNKNOWN = -1  # centiamps
REMAINING_UNKNOWN = -1  # percent

BATTERY_UNAVAILABLE = {"voltage_v": None, "current_a": None, "remaining_percent": None}
ATTITUDE_UNAVAILABLE = {"roll_deg": None, "pitch_deg": None, "yaw_deg": None}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
class ComponentSnapshot:
    configured: bool
    connected: bool
    detail: str
    checked_at: datetime


class VehicleStatusService:
    def __init__(
        self,
        *,
        motor_device: str | None = DEVICE,
        fc_device: str | None = FC_DEVICE,
        fc_baudrate: int = FC_BAUDRATE,
        probe_interval_seconds: float = 2.0,
        motor_factory: Callable[..., DDS115] = DDS115,
        fc_factory: Callable[..., Any] = mavutil.mavlink_connection,
        fc_read_seconds: float = 0.6,
        sleep_func: Callable[[float], None] = sleep,
        pi_health_reader: Callable[[], dict[str, Any]] | None = None,
    ):
        self.motor_device = motor_device
        self.fc_device = fc_device
        self.fc_baudrate = fc_baudrate
        self.probe_interval_seconds = probe_interval_seconds
        self._motor_factory = motor_factory
        self._fc_factory = fc_factory
        self.fc_read_seconds = fc_read_seconds
        self._sleep = sleep_func
        self._read_pi_health = pi_health_reader or PiHealthReader()
        self._stop_event = Event()
        self._lock = Lock()
        self._motor_bus_lock = Lock()
        self._thread: Thread | None = None
        self._current_command_rpm = 0
        self._components = {
            "motor_bus": ComponentSnapshot(
                configured=bool(self.motor_device),
                connected=False,
                detail="Probe has not run yet",
                checked_at=utc_now(),
            ),
            "flight_controller": ComponentSnapshot(
                configured=bool(self.fc_device),
                connected=False,
                detail="Probe has not run yet",
                checked_at=utc_now(),
            ),
        }
        self._motor_feedback = [
            {"motor_id": motor_id, "rpm": None, "current_raw": None}
            for motor_id in LEFT_SIDE + RIGHT_SIDE
        ]
        self._battery = dict(BATTERY_UNAVAILABLE)
        self._attitude = dict(ATTITUDE_UNAVAILABLE)
        self._pi = dict(PI_HEALTH_UNAVAILABLE)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = Thread(target=self._probe_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.probe_interval_seconds + 1.0)
            self._thread = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            components = {name: asdict(component) for name, component in self._components.items()}
            motor_feedback = list(self._motor_feedback)
            battery = dict(self._battery)
            attitude = dict(self._attitude)
            pi = dict(self._pi)

        return {
            "service": "mt0d12-vehicle-api",
            "overall_status": self._overall_status(components),
            "timestamp": utc_now(),
            "motor_device": self.motor_device,
            "fc_device": self.fc_device,
            "motor_ids": {
                "left": list(LEFT_SIDE),
                "right": list(RIGHT_SIDE),
            },
            "components": components,
            "battery": battery,
            "attitude": attitude,
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

    def _probe_loop(self) -> None:
        while not self._stop_event.is_set():
            self._probe_once()
            self._stop_event.wait(self.probe_interval_seconds)

    def _probe_once(self) -> None:
        """One pass over the hardware. Separate from the loop so it can be run
        a single time - by a test, or by anything that wants a fresh reading
        without waiting for the next tick."""
        motor_bus = self._probe_motor_bus()
        flight_controller, battery, attitude = self._probe_flight_controller()
        pi = self._probe_pi()

        with self._lock:
            self._pi = pi
            self._components["motor_bus"] = motor_bus
            self._components["flight_controller"] = flight_controller
            if not flight_controller.connected:
                # A reading from a link that has since dropped would read as
                # current. Nothing known beats quietly wrong.
                self._battery = dict(BATTERY_UNAVAILABLE)
                self._attitude = dict(ATTITUDE_UNAVAILABLE)
            else:
                # SYS_STATUS streams slower than the probe runs, so a window
                # that caught none keeps the last reading rather than blanking
                # a battery that is still there.
                if battery is not None:
                    self._battery = battery
                if attitude is not None:
                    self._attitude = attitude

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

    def _probe_flight_controller(
        self,
    ) -> tuple[ComponentSnapshot, dict[str, Any] | None, dict[str, Any] | None]:
        """Probe the link and, while it is open, read what it has to say.

        Returns the component health plus whatever battery and attitude
        arrived in the read window - `None` for either means "nothing this
        time", which the caller treats differently from "nothing there".
        """
        checked_at = utc_now()
        if not self.fc_device:
            return (
                ComponentSnapshot(
                    configured=False,
                    connected=False,
                    detail="FC_DEVICE is not configured",
                    checked_at=checked_at,
                ),
                None,
                None,
            )

        link = None
        try:
            link = self._fc_factory(self.fc_device, baud=self.fc_baudrate)
            heartbeat = link.wait_heartbeat(timeout=FC_HEARTBEAT_TIMEOUT_SECONDS)
            if heartbeat is None:
                # wait_heartbeat returns None on timeout rather than raising,
                # so a port that opens but never speaks has to be caught here.
                return (
                    ComponentSnapshot(
                        configured=True,
                        connected=False,
                        detail=f"No heartbeat within {FC_HEARTBEAT_TIMEOUT_SECONDS:g}s",
                        checked_at=checked_at,
                    ),
                    None,
                    None,
                )
            battery, attitude = self._read_fc_telemetry(link)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            return (
                ComponentSnapshot(
                    configured=True,
                    connected=False,
                    detail=str(exc),
                    checked_at=checked_at,
                ),
                None,
                None,
            )
        finally:
            if link is not None and hasattr(link, "close"):
                link.close()

        system_id = getattr(heartbeat, "get_srcSystem", lambda: None)()
        return (
            ComponentSnapshot(
                configured=True,
                connected=True,
                detail=f"Heartbeat received from system {system_id}",
                checked_at=checked_at,
            ),
            battery,
            attitude,
        )

    def _read_fc_telemetry(self, link: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """One SYS_STATUS and one ATTITUDE, or as much as arrives in time.

        The probe reopens the link every couple of seconds, so this cannot sit
        and wait: anything that has not arrived by the deadline is left for the
        next probe. Reading must never cost the health check, so a malformed
        frame ends the window rather than failing the probe.
        """
        deadline = monotonic() + self.fc_read_seconds
        battery: dict[str, Any] | None = None
        attitude: dict[str, Any] | None = None

        while battery is None or attitude is None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            try:
                message = link.recv_match(
                    type=["SYS_STATUS", "ATTITUDE"], blocking=True, timeout=remaining
                )
                if message is None:
                    break
                kind = message.get_type()
                if kind == "SYS_STATUS" and battery is None:
                    battery = battery_from(message)
                elif kind == "ATTITUDE" and attitude is None:
                    attitude = attitude_from(message)
            except Exception as error:
                logger.debug("Flight controller telemetry read failed", exc_info=error)
                break

        return battery, attitude

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
        for motor_id in LEFT_SIDE:
            motor.send_rpm(motor_id, rpm=base_rpm)
        for motor_id in RIGHT_SIDE:
            motor.send_rpm(motor_id, rpm=base_rpm * (-1))

        with self._lock:
            self._current_command_rpm = base_rpm
            self._motor_feedback = [
                {
                    "motor_id": motor_id,
                    "rpm": base_rpm if motor_id in LEFT_SIDE else base_rpm * (-1),
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
