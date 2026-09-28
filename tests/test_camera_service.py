import asyncio
import threading
import time

import pytest

from apps.api.services.camera import CameraService


class FakeCameraBackend:
    """Stands in for picamera2: pushes frames only when the test asks for one."""

    instances: list["FakeCameraBackend"] = []

    def __init__(
        self,
        *,
        width: int,
        height: int,
        framerate: int,
        jpeg_quality: int,
        encoder: str = "auto",
        buffer_count: int = 2,
    ):
        self.width = width
        self.height = height
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.encoder = encoder
        self.buffer_count = buffer_count
        self.active_encoder = "software"
        self.started = False
        self.fail_on_start: Exception | None = None
        self._on_frame = None
        FakeCameraBackend.instances.append(self)

    def start(self, on_frame) -> None:
        if self.fail_on_start is not None:
            raise self.fail_on_start
        self._on_frame = on_frame
        self.started = True

    def stop(self) -> None:
        self.started = False

    def push(self, frame: bytes) -> None:
        self._on_frame(frame)


@pytest.fixture(autouse=True)
def clear_backends():
    FakeCameraBackend.instances.clear()
    yield
    FakeCameraBackend.instances.clear()


# `CameraService` binds its defaults from `settings` at import, so a service
# built without arguments inherits whatever `.env` the machine happens to have.
# These pin the camera the tests talk about: a test that changes its meaning
# with the checkout it runs in is not testing the code.
CAMERA = {
    "enabled": True,
    "width": 640,
    "height": 480,
    "framerate": 20,
    "jpeg_quality": 80,
    "max_clients": 4,
    "encoder": "auto",
    "buffer_count": 2,
}


def make_service(**kwargs) -> CameraService:
    return CameraService(
        backend_factory=FakeCameraBackend,
        **{"frame_timeout_seconds": 0.2, "idle_stop_seconds": 0.05, **CAMERA, **kwargs},
    )


def test_start_is_idempotent() -> None:
    service = make_service()

    service.start()
    service.start()

    assert len(FakeCameraBackend.instances) == 1
    assert service.snapshot()["running"] is True


def test_stop_releases_the_backend() -> None:
    service = make_service()
    service.start()
    backend = FakeCameraBackend.instances[0]

    result = service.stop()

    assert backend.started is False
    assert result["action"] == "stop"
    assert service.snapshot()["running"] is False


def test_disabled_camera_refuses_to_start() -> None:
    service = make_service(enabled=False)

    with pytest.raises(RuntimeError, match="disabled by configuration"):
        service.start()


def test_backend_failure_is_reported_on_the_component() -> None:
    service = make_service()
    service._backend_factory = _failing_factory

    with pytest.raises(RuntimeError, match="Camera is unavailable"):
        service.start()

    component = service.snapshot()["component"]
    assert component["connected"] is False
    assert "no camera" in component["detail"]


def _failing_factory(**kwargs):
    backend = FakeCameraBackend(**kwargs)
    backend.fail_on_start = RuntimeError("no camera detected")
    return backend


def test_encoder_settings_reach_the_backend_and_the_status() -> None:
    service = make_service(encoder="hardware", buffer_count=3)
    service.start()
    backend = FakeCameraBackend.instances[0]

    assert backend.encoder == "hardware"
    assert backend.buffer_count == 3
    # The backend reports what it actually negotiated, not what was requested.
    assert service.snapshot()["encoder"] == "software"


def test_next_frame_returns_the_latest_frame_and_drops_stale_ones() -> None:
    service = make_service()
    service.acquire_client_slot()
    backend = FakeCameraBackend.instances[0]

    backend.push(b"first")
    backend.push(b"second")  # overwrites the frame nobody read yet
    seq, frame = asyncio.run(service.next_frame(-1))
    assert frame == b"second"

    backend.push(b"third")
    assert asyncio.run(service.next_frame(seq))[1] == b"third"


def test_a_frame_from_the_capture_thread_wakes_a_waiting_viewer() -> None:
    service = make_service(frame_timeout_seconds=2.0)
    service.acquire_client_slot()
    backend = FakeCameraBackend.instances[0]

    async def main():
        waiting = asyncio.create_task(service.next_frame(-1))
        await asyncio.sleep(0.05)
        assert not waiting.done(), "must wait while there is no frame yet"
        # The real backend publishes from picamera2's own thread.
        threading.Thread(target=backend.push, args=(b"fresh",)).start()
        return await waiting

    assert asyncio.run(main())[1] == b"fresh"


def test_stop_ends_a_waiting_viewer() -> None:
    service = make_service(frame_timeout_seconds=2.0)
    service.acquire_client_slot()

    async def main():
        waiting = asyncio.create_task(service.next_frame(-1))
        await asyncio.sleep(0.05)
        await asyncio.to_thread(service.stop)
        return await waiting

    assert asyncio.run(main()) is None


def test_next_frame_times_out_and_marks_the_camera_disconnected() -> None:
    service = make_service()
    service.start()

    with pytest.raises(RuntimeError, match="Timed out"):
        asyncio.run(service.next_frame(-1))

    component = service.snapshot()["component"]
    assert component["connected"] is False
    assert "No frame received" in component["detail"]


def test_client_slots_are_capped_and_released() -> None:
    service = make_service(max_clients=1)
    service.acquire_client_slot()

    with pytest.raises(RuntimeError, match="already has 1 of 1 viewers"):
        service.acquire_client_slot()

    service.release_client_slot()
    assert service.snapshot()["clients"] == 0
    service.acquire_client_slot()


def test_next_frame_returns_none_once_the_camera_is_stopped() -> None:
    service = make_service()
    service.start()
    FakeCameraBackend.instances[0].push(b"frame")

    service.stop()

    assert asyncio.run(service.next_frame(-1)) is None


def test_camera_stops_once_the_last_viewer_leaves() -> None:
    service = make_service()
    service.acquire_client_slot()
    service.acquire_client_slot()
    backend = FakeCameraBackend.instances[0]

    service.release_client_slot()
    time.sleep(0.15)
    assert backend.started is True, "a viewer is still watching"

    service.release_client_slot()
    time.sleep(0.15)
    assert backend.started is False
    assert service.snapshot()["running"] is False


def test_a_viewer_returning_within_the_grace_period_keeps_the_camera() -> None:
    service = make_service(idle_stop_seconds=0.1)
    service.acquire_client_slot()
    service.release_client_slot()

    service.acquire_client_slot()
    time.sleep(0.2)

    assert len(FakeCameraBackend.instances) == 1
    assert FakeCameraBackend.instances[0].started is True


def test_capture_frame_waits_for_a_fresh_frame() -> None:
    service = make_service()
    service.start()
    backend = FakeCameraBackend.instances[0]
    backend.push(b"stale")

    with pytest.raises(RuntimeError, match="Timed out"):
        service.capture_frame()


def test_snapshot_reports_capture_counters() -> None:
    service = make_service()
    service.start()
    backend = FakeCameraBackend.instances[0]
    backend.push(b"frame")

    snapshot = service.snapshot()

    assert snapshot["frames_captured"] == 1
    assert snapshot["last_frame_at"] is not None
    assert snapshot["width"] == CAMERA["width"]
    assert snapshot["component"]["connected"] is True
