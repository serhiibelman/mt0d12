from datetime import datetime, timezone

import pytest

from apps.api.main import create_app


class FakeVehicleStatusService:
    def __init__(self) -> None:
        self.started_rpms: list[int] = []
        self.stop_calls = 0
        # Settable so a test can watch a change arrive over /ws/status.
        self.voltage = 12.4

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def start_motors(self, rpm: int) -> dict:
        self.started_rpms.append(rpm)
        now = datetime.now(timezone.utc)
        return {
            "service": "r2d2-vehicle-api",
            "action": "start",
            "target_rpm": rpm,
            "current_rpm": rpm,
            "detail": f"All motors ramped to {rpm} rpm",
            "timestamp": now,
        }

    def stop_motors(self) -> dict:
        self.stop_calls += 1
        now = datetime.now(timezone.utc)
        return {
            "service": "r2d2-vehicle-api",
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
            "service": "r2d2-vehicle-api",
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

    def start(self) -> dict:
        self._guard()
        self.start_calls += 1
        return self._command("start", "Camera capture is running", running=True)

    def stop(self) -> dict:
        self.stop_calls += 1
        return self._command("stop", "Camera capture stopped", running=False)

    def acquire_client_slot(self) -> None:
        self._guard()
        self.slots += 1

    def release_client_slot(self) -> None:
        self.slots -= 1

    def next_frame(self, last_seq: int):
        frames = [b"first", b"second"]
        next_seq = last_seq + 1
        if next_seq >= len(frames):
            return None
        return next_seq, frames[next_seq]

    def capture_frame(self) -> bytes:
        self._guard()
        return b"jpeg-bytes"

    def snapshot(self) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "service": "r2d2-vehicle-api",
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
            "service": "r2d2-vehicle-api",
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
    def _build(vehicle=None, camera=None):
        return create_app(
            vehicle_status_service=vehicle or vehicle_service,
            camera_service=camera or camera_service,
        )

    return _build
