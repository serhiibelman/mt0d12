import asyncio
import threading

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
        """A frame from the encoder. The real one calls from picamera2's own
        thread, and the hop onto the loop is the service's job either way."""
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
    defaults = {
        "backend_factory": FakeCameraBackend,
        "frame_timeout_seconds": 0.2,
        "idle_stop_seconds": 0.05,
    }
    return CameraService(**{**defaults, **CAMERA, **kwargs})


def run(scenario) -> None:
    """Every camera call is on the event loop now; each test is one run."""
    asyncio.run(scenario())


async def pushed(backend: FakeCameraBackend, frame: bytes) -> None:
    """Push a frame and let the loop take it, as it would between two awaits."""
    backend.push(frame)
    await asyncio.sleep(0)


def test_start_is_idempotent() -> None:
    async def scenario():
        service = make_service()

        await service.start()
        await service.start()

        assert len(FakeCameraBackend.instances) == 1
        assert service.snapshot()["running"] is True

    run(scenario)


def test_viewers_arriving_together_share_one_open() -> None:
    async def scenario():
        service = make_service()

        await asyncio.gather(*(service.acquire_client_slot() for _ in range(3)))

        assert len(FakeCameraBackend.instances) == 1
        assert service.snapshot()["clients"] == 3

    run(scenario)


def test_stop_releases_the_backend() -> None:
    async def scenario():
        service = make_service()
        await service.start()
        backend = FakeCameraBackend.instances[0]

        result = await service.stop()

        assert backend.started is False
        assert result["action"] == "stop"
        assert service.snapshot()["running"] is False

    run(scenario)


def test_disabled_camera_refuses_to_start() -> None:
    async def scenario():
        service = make_service(enabled=False)

        with pytest.raises(RuntimeError, match="disabled by configuration"):
            await service.start()

    run(scenario)


def test_backend_failure_is_reported_on_the_component() -> None:
    async def scenario():
        service = make_service()
        service._backend_factory = _failing_factory

        with pytest.raises(RuntimeError, match="Camera is unavailable"):
            await service.start()

        component = service.snapshot()["component"]
        assert component["connected"] is False
        assert "no camera" in component["detail"]
        # A failed open is not remembered: the next viewer tries again.
        service._backend_factory = FakeCameraBackend
        await service.start()
        assert service.snapshot()["running"] is True

    run(scenario)


def _failing_factory(**kwargs):
    backend = FakeCameraBackend(**kwargs)
    backend.fail_on_start = RuntimeError("no camera detected")
    return backend


def test_a_viewer_that_fails_to_open_the_camera_gives_its_slot_back() -> None:
    async def scenario():
        service = make_service(max_clients=1)
        service._backend_factory = _failing_factory

        with pytest.raises(RuntimeError, match="Camera is unavailable"):
            await service.acquire_client_slot()

        assert service.snapshot()["clients"] == 0

    run(scenario)


def test_encoder_settings_reach_the_backend_and_the_status() -> None:
    async def scenario():
        service = make_service(encoder="hardware", buffer_count=3)
        await service.start()
        backend = FakeCameraBackend.instances[0]

        assert backend.encoder == "hardware"
        assert backend.buffer_count == 3
        # The backend reports what it actually negotiated, not what was requested.
        assert service.snapshot()["encoder"] == "software"

    run(scenario)


def test_next_frame_returns_the_latest_frame_and_drops_stale_ones() -> None:
    async def scenario():
        service = make_service()
        await service.acquire_client_slot()
        backend = FakeCameraBackend.instances[0]

        backend.push(b"first")
        await pushed(backend, b"second")  # overwrites the frame nobody read yet
        seq, frame = await service.next_frame(-1)
        assert frame == b"second"

        await pushed(backend, b"third")
        assert (await service.next_frame(seq))[1] == b"third"

    run(scenario)


def test_a_frame_from_the_capture_thread_wakes_a_waiting_viewer() -> None:
    async def scenario():
        service = make_service(frame_timeout_seconds=2.0)
        await service.acquire_client_slot()
        backend = FakeCameraBackend.instances[0]

        waiting = asyncio.create_task(service.next_frame(-1))
        await asyncio.sleep(0.05)
        assert not waiting.done(), "must wait while there is no frame yet"
        # The real backend publishes from picamera2's own thread.
        threading.Thread(target=backend.push, args=(b"fresh",)).start()
        assert (await waiting)[1] == b"fresh"

    run(scenario)


def test_stop_ends_a_waiting_viewer() -> None:
    async def scenario():
        service = make_service(frame_timeout_seconds=2.0)
        await service.acquire_client_slot()

        waiting = asyncio.create_task(service.next_frame(-1))
        await asyncio.sleep(0.05)
        await service.stop()
        assert await waiting is None

    run(scenario)


def test_a_frame_the_closed_camera_was_still_encoding_is_dropped() -> None:
    async def scenario():
        service = make_service()
        await service.start()
        old = FakeCameraBackend.instances[0]
        await service.stop()
        await service.start()

        # picamera2 can hand over one last frame after stop_recording.
        await pushed(old, b"from before the stop")

        assert service.snapshot()["frames_captured"] == 0
        with pytest.raises(RuntimeError, match="Timed out"):
            await service.next_frame(-1)

    run(scenario)


def test_next_frame_times_out_and_marks_the_camera_disconnected() -> None:
    async def scenario():
        service = make_service()
        await service.start()

        with pytest.raises(RuntimeError, match="Timed out"):
            await service.next_frame(-1)

        component = service.snapshot()["component"]
        assert component["connected"] is False
        assert "No frame received" in component["detail"]

    run(scenario)


def test_client_slots_are_capped_and_released() -> None:
    async def scenario():
        service = make_service(max_clients=1)
        await service.acquire_client_slot()

        with pytest.raises(RuntimeError, match="already has 1 of 1 viewers"):
            await service.acquire_client_slot()

        await service.release_client_slot()
        assert service.snapshot()["clients"] == 0
        await service.acquire_client_slot()

    run(scenario)


def test_next_frame_returns_none_once_the_camera_is_stopped() -> None:
    async def scenario():
        service = make_service()
        await service.start()
        await pushed(FakeCameraBackend.instances[0], b"frame")

        await service.stop()

        assert await service.next_frame(-1) is None

    run(scenario)


def test_camera_stops_once_the_last_viewer_leaves() -> None:
    async def scenario():
        service = make_service()
        await service.acquire_client_slot()
        await service.acquire_client_slot()
        backend = FakeCameraBackend.instances[0]

        await service.release_client_slot()
        await asyncio.sleep(0.15)
        assert backend.started is True, "a viewer is still watching"

        await service.release_client_slot()
        await asyncio.sleep(0.15)
        assert backend.started is False
        assert service.snapshot()["running"] is False

    run(scenario)


def test_a_viewer_returning_within_the_grace_period_keeps_the_camera() -> None:
    async def scenario():
        service = make_service(idle_stop_seconds=0.1)
        await service.acquire_client_slot()
        await service.release_client_slot()

        await service.acquire_client_slot()
        await asyncio.sleep(0.2)

        assert len(FakeCameraBackend.instances) == 1
        assert FakeCameraBackend.instances[0].started is True

    run(scenario)


def test_a_viewer_that_leaves_mid_open_does_not_leave_the_camera_on() -> None:
    async def scenario():
        opening = threading.Event()

        def slow_factory(**kwargs):
            backend = FakeCameraBackend(**kwargs)
            start = backend.start

            def slow_start(on_frame):
                opening.wait()
                start(on_frame)

            backend.start = slow_start
            return backend

        service = make_service(backend_factory=slow_factory)
        viewer = asyncio.create_task(service.acquire_client_slot())
        await asyncio.sleep(0.02)
        viewer.cancel()  # the browser went away while the sensor powered up
        with pytest.raises(asyncio.CancelledError):
            await viewer

        opening.set()  # the open carries on in its thread and finishes
        await asyncio.sleep(0.15)

        assert FakeCameraBackend.instances[0].started is False
        assert service.snapshot()["running"] is False
        assert service.snapshot()["clients"] == 0

    run(scenario)


def test_capture_frame_waits_for_a_fresh_frame() -> None:
    async def scenario():
        service = make_service()
        await service.start()
        await pushed(FakeCameraBackend.instances[0], b"stale")

        with pytest.raises(RuntimeError, match="Timed out"):
            await service.capture_frame()

    run(scenario)


def test_capture_frame_returns_the_next_frame_and_lets_the_camera_close() -> None:
    async def scenario():
        service = make_service()

        async def push_soon():
            await asyncio.sleep(0.02)
            FakeCameraBackend.instances[0].push(b"fresh")

        pusher = asyncio.create_task(push_soon())
        assert await service.capture_frame() == b"fresh"
        await pusher

        await asyncio.sleep(0.15)
        assert service.snapshot()["running"] is False

    run(scenario)


def test_snapshot_reports_capture_counters() -> None:
    async def scenario():
        service = make_service()
        await service.start()
        await pushed(FakeCameraBackend.instances[0], b"frame")

        snapshot = service.snapshot()

        assert snapshot["frames_captured"] == 1
        assert snapshot["last_frame_at"] is not None
        assert snapshot["width"] == CAMERA["width"]
        assert snapshot["component"]["connected"] is True

    run(scenario)
