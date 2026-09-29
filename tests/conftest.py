import asyncio
import contextlib
import time
from datetime import datetime, timezone

import pytest

from apps.api.main import create_app
from apps.api.schemas import VehicleStatusResponse
from apps.api.services.status_broadcaster import StatusBroadcaster
from lib.ddsm115 import MotorBus


class FakeMotorBus:
    """The rover's one motor bus: who holds it, and what they sent."""

    def __init__(self) -> None:
        self.device = "/dev/ttyACM0"
        self.holder: object | None = None
        self.commands: list[tuple[int, int]] = []
        self.releases = 0
        self.error: str | None = None

    def claim(self, owner: object, name: str) -> None:
        if self.holder is not None and self.holder is not owner:
            raise RuntimeError(f"{self.holder_name} is driving")
        self.holder, self.holder_name = owner, name

    async def release(self, owner: object, *, stop: bool = True) -> None:
        if self.holder is None or owner is not self.holder:
            return
        self.holder = None
        self.releases += 1

    async def drive(self, owner: object, left_rpm: int, right_rpm: int) -> None:
        if owner is not self.holder:
            return
        if self.error is not None:
            raise RuntimeError(self.error)
        self.commands.append((left_rpm, right_rpm))


class FakeVehicleStatusService:
    def __init__(self) -> None:
        self.started_rpms: list[int] = []
        self.stop_calls = 0
        # Settable so a test can watch a change arrive over /ws/status.
        self.voltage = 12.4
        self.bus = FakeMotorBus()

    async def start_motors(self, rpm: int) -> dict:
        self.started_rpms.append(rpm)
        now = datetime.now(timezone.utc)
        return {
            "service": "mt0d12-vehicle-api",
            "action": "start",
            "target_rpm": rpm,
            "current_rpm": rpm,
            "detail": f"All motors ramped to {rpm} rpm",
            "timestamp": now,
        }

    async def stop_motors(self) -> dict:
        self.stop_calls += 1
        now = datetime.now(timezone.utc)
        return {
            "service": "mt0d12-vehicle-api",
            "action": "stop",
            "target_rpm": 0,
            "current_rpm": 0,
            "detail": "All motors ramped down to 0 rpm",
            "timestamp": now,
        }

    def snapshot(self) -> dict:
        now = datetime.now(timezone.utc)
        components = {
            "motor_bus": {
                "configured": True,
                "connected": True,
                "detail": "ok",
                "checked_at": now,
            },
            "flight_controller": {
                "configured": True,
                "connected": False,
                "detail": "timeout",
                "checked_at": now,
            },
        }
        return {
            "service": "mt0d12-vehicle-api",
            "overall_status": "degraded",
            "timestamp": now,
            "motor_device": "/dev/ttyACM0",
            "fc_device": "/dev/serial0",
            "motor_ids": {"left": [3, 4], "right": [1, 2]},
            "components": components,
            "battery": {"voltage_v": self.voltage, "current_a": 1.83, "remaining_percent": 76},
            "attitude": {"roll_deg": 0.4, "pitch_deg": -1.2, "yaw_deg": 271.3},
            "pi": {
                "cpu_temp_c": 51.5,
                "load_1m": 0.42,
                "memory_available_mb": 210,
                "memory_available_percent": 48,
                "disk_free_mb": 5120,
                "disk_free_percent": 62,
                "throttled_raw": "0x50000",
                "undervoltage_now": False,
                "freq_capped_now": False,
                "throttled_now": False,
                "soft_temp_limit_now": False,
                "undervoltage_since_boot": True,
                "freq_capped_since_boot": False,
                "throttled_since_boot": True,
                "soft_temp_limit_since_boot": False,
                "warnings": [],
            },
            "motor_feedback": [
                {"motor_id": 1, "rpm": None, "current_raw": None},
                {"motor_id": 2, "rpm": None, "current_raw": None},
            ],
        }


class FakeCameraService:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.start_calls = 0
        self.stop_calls = 0
        self.slots = 0

    def _guard(self) -> None:
        if not self.available:
            raise RuntimeError("Camera is unavailable: no camera detected")

    async def start(self) -> dict:
        self._guard()
        self.start_calls += 1
        return self._command("start", "Camera capture is running", running=True)

    async def stop(self) -> dict:
        self.stop_calls += 1
        return self._command("stop", "Camera capture stopped", running=False)

    def preload(self) -> None:
        pass

    async def acquire_client_slot(self) -> None:
        self._guard()
        self.slots += 1

    async def release_client_slot(self) -> None:
        self.slots -= 1

    async def next_frame(self, last_seq: int):
        frames = [b"first", b"second"]
        next_seq = last_seq + 1
        if next_seq >= len(frames):
            return None
        return next_seq, frames[next_seq]

    async def capture_frame(self) -> bytes:
        self._guard()
        return b"jpeg-bytes"

    def snapshot(self) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "service": "mt0d12-vehicle-api",
            "timestamp": now,
            "running": self.available,
            "clients": 0,
            "width": 640,
            "height": 480,
            "framerate": 20,
            "jpeg_quality": 80,
            "encoder": "hardware",
            "frames_captured": 12,
            "last_frame_at": now,
            "component": {
                "configured": True,
                "connected": self.available,
                "detail": "ok",
                "checked_at": now,
            },
        }

    @staticmethod
    def _command(action: str, detail: str, *, running: bool) -> dict:
        return {
            "service": "mt0d12-vehicle-api",
            "action": action,
            "running": running,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc),
        }


@pytest.fixture()
def vehicle_service() -> FakeVehicleStatusService:
    return FakeVehicleStatusService()


@pytest.fixture()
def camera_service() -> FakeCameraService:
    return FakeCameraService()


@pytest.fixture()
def make_camera_service():
    return FakeCameraService


@pytest.fixture()
def build_app(vehicle_service, camera_service):
    def _build(vehicle=None, camera=None, drive_link_timeout=0.5):
        vehicle = vehicle or vehicle_service
        return create_app(
            drive_link_timeout=drive_link_timeout,
            vehicle_status_service=vehicle,
            camera_service=camera or camera_service,
            # 5 Hz is right for a browser; a test does not need to sit through it.
            status_broadcaster=StatusBroadcaster(
                render=lambda: VehicleStatusResponse.from_snapshot(vehicle.snapshot()).json(),
                interval=0.01,
            ),
        )

    return _build


# -- a real MotorBus over fake motors ------------------------------------------


class FakeMotor:
    """A DDS115 on the far end of the port: records commands, can hold the bus
    for `delay` per command like a real motor, and can fail like a pulled
    cable."""

    def __init__(self, device: str, delay: float = 0.0) -> None:
        self.device = device
        self.delay = delay
        self.commands: list[tuple[int, object]] = []
        self.closed = False
        self.error: Exception | None = None

    def send_rpm(self, motor_id: int, rpm: int = 0) -> None:
        self._command(motor_id, rpm)

    def set_brake(self, motor_id: int) -> None:
        self._command(motor_id, "brake")

    def _command(self, motor_id: int, value: object) -> None:
        if self.error is not None:
            raise self.error
        time.sleep(self.delay)
        self.commands.append((motor_id, value))

    def close(self) -> None:
        self.closed = True


async def settle(condition, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.002)


@pytest.fixture()
def running_bus():
    """`async with running_bus() as (bus, motors)`: a MotorBus with its port
    open, over fake motors - one per open, newest last."""

    @contextlib.asynccontextmanager
    async def _running(delay: float = 0.0, **kwargs):
        motors: list[FakeMotor] = []

        def factory(*, device: str) -> FakeMotor:
            motor = FakeMotor(device, delay)
            motors.append(motor)
            return motor

        options = {
            "device": "/dev/test",
            "motor_factory": factory,
            "device_present": lambda _: True,
            "check_interval": 0.01,
            "backoff_min": 0.01,
            "backoff_max": 0.02,
            **kwargs,
        }
        bus = MotorBus(**options)
        task = asyncio.create_task(bus.run())
        try:
            await settle(lambda: bus.is_open)
            yield bus, motors
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return _running
