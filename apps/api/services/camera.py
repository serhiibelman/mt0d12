from __future__ import annotations

import asyncio
import io
from threading import Condition, RLock, Timer
from typing import Any, Callable

from apps.api.services.components import ComponentSnapshot, utc_now
from settings import (
    CAMERA_BUFFER_COUNT,
    CAMERA_ENABLED,
    CAMERA_ENCODER,
    CAMERA_FRAMERATE,
    CAMERA_HEIGHT,
    CAMERA_JPEG_QUALITY,
    CAMERA_MAX_CLIENTS,
    CAMERA_WIDTH,
)

BACKEND_ERRORS = (ImportError, OSError, RuntimeError, ValueError)


class _FrameSink(io.BufferedIOBase):
    """File-like object picamera2 writes each encoded JPEG into."""

    def __init__(self, on_frame: Callable[[bytes], None]):
        self._on_frame = on_frame

    def writable(self) -> bool:
        return True

    def write(self, buffer: Any) -> int:
        frame = bytes(buffer)
        self._on_frame(frame)
        return len(frame)


HARDWARE_ENCODER = "hardware"
SOFTWARE_ENCODER = "software"


class Picamera2Backend:
    """
    MJPEG capture from the RPi Camera (B) (OV5647) through picamera2/libcamera.

    Prefers the VideoCore JPEG encoder over the software one. That matters on
    ARMv6 boards such as the Pi 1, where software encoding has no SIMD to lean
    on and would eat the only core the motor loop also runs on. Boards without a
    hardware JPEG block (Pi 5) fall back to software automatically.
    """

    def __init__(
        self,
        *,
        width: int,
        height: int,
        framerate: int,
        jpeg_quality: int,
        encoder: str = CAMERA_ENCODER,
        buffer_count: int = CAMERA_BUFFER_COUNT,
    ):
        self.width = width
        self.height = height
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.encoder = encoder
        self.buffer_count = buffer_count
        self.active_encoder: str | None = None
        self._camera = None

    def start(self, on_frame: Callable[[bytes], None]) -> None:
        # Imported lazily so the API still boots on machines without libcamera.
        from picamera2 import Picamera2
        from picamera2.encoders import JpegEncoder, MJPEGEncoder
        from picamera2.outputs import FileOutput

        frame_duration = int(1_000_000 / self.framerate)
        sink = _FrameSink(on_frame)
        last_error: Exception | None = None

        for mode in self._encoder_candidates():
            hardware = mode == HARDWARE_ENCODER
            main: dict[str, Any] = {"size": (self.width, self.height)}
            if hardware:
                # The V4L2 encoder consumes YUV420 directly, which skips a colour
                # conversion the Pi 1 cannot afford.
                main["format"] = "YUV420"

            camera = Picamera2()
            try:
                camera.configure(
                    camera.create_video_configuration(
                        main=main,
                        buffer_count=self.buffer_count,
                        controls={"FrameDurationLimits": (frame_duration, frame_duration)},
                    )
                )
                camera.start_recording(
                    (
                        MJPEGEncoder(bitrate=self._bitrate())
                        if hardware
                        else JpegEncoder(q=self.jpeg_quality)
                    ),
                    FileOutput(sink),
                )
            except BACKEND_ERRORS as exc:
                last_error = exc
                camera.close()
                continue

            self._camera = camera
            self.active_encoder = mode
            return

        raise last_error if last_error else RuntimeError("No usable JPEG encoder")

    def _encoder_candidates(self) -> list[str]:
        if self.encoder in (HARDWARE_ENCODER, SOFTWARE_ENCODER):
            return [self.encoder]
        return [HARDWARE_ENCODER, SOFTWARE_ENCODER]

    def _bitrate(self) -> int:
        """Approximate the requested JPEG quality as a bitrate for the V4L2 encoder."""
        bits_per_pixel = (self.jpeg_quality / 100) * 1.2
        return max(500_000, int(self.width * self.height * self.framerate * bits_per_pixel))

    def stop(self) -> None:
        camera, self._camera = self._camera, None
        if camera is None:
            return
        try:
            camera.stop_recording()
        finally:
            camera.close()


class CameraService:
    """Owns the camera and fans the newest frame out to every viewer.

    A single capture thread inside the backend publishes frames here; readers
    always get the latest one and silently drop whatever they missed, so a slow
    viewer can never stall capture.

    Stream viewers wait on the event loop, not in worker threads: each frame
    costs one `call_soon_threadsafe` hop however many viewers there are, and a
    viewer holds no threadpool slot while it waits. Once the last viewer has
    gone for `idle_stop_seconds` the camera shuts down, so the sensor is only
    powered while someone is watching.
    """

    def __init__(
        self,
        *,
        enabled: bool = CAMERA_ENABLED,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        framerate: int = CAMERA_FRAMERATE,
        jpeg_quality: int = CAMERA_JPEG_QUALITY,
        max_clients: int = CAMERA_MAX_CLIENTS,
        encoder: str = CAMERA_ENCODER,
        buffer_count: int = CAMERA_BUFFER_COUNT,
        frame_timeout_seconds: float = 5.0,
        idle_stop_seconds: float = 2.0,
        backend_factory: Callable[..., Picamera2Backend] = Picamera2Backend,
    ):
        self.enabled = enabled
        self.width = width
        self.height = height
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.max_clients = max_clients
        self.encoder = encoder
        self.buffer_count = buffer_count
        self.frame_timeout_seconds = frame_timeout_seconds
        self.idle_stop_seconds = idle_stop_seconds
        self.active_encoder: str | None = None
        self._backend_factory = backend_factory
        self._backend: Picamera2Backend | None = None
        self._state_lock = RLock()
        self._frame_condition = Condition()
        self._frame: bytes | None = None
        self._frame_seq = 0
        self._frames_captured = 0
        self._last_frame_at = None
        self._clients = 0
        self._running = False
        self._idle_timer: Timer | None = None
        # Bound by the first async viewer. The capture thread wakes viewers
        # through it; `_frame_ready` is only ever touched on that loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._frame_ready: asyncio.Event | None = None
        self._component = ComponentSnapshot(
            configured=enabled,
            connected=False,
            detail="Camera has not been started yet",
            checked_at=utc_now(),
        )

    def preload(self) -> None:
        """Import picamera2 now, so the first viewer does not pay for it.

        On a Pi 1 the import (numpy, libcamera) takes several seconds of the
        only core. Done on the first stream - the moment the page arms - it
        starved the event loop long enough for the drive link to time out.
        Blocking, so the app runs it in a thread at startup.
        """
        if not self.enabled:
            return
        try:
            import picamera2  # noqa: F401
        except BACKEND_ERRORS:
            # The first real start reports it; this is only a head start.
            pass

    def start(self) -> dict[str, Any]:
        self.ensure_running()
        return self._command_response(action="start", detail="Camera capture is running")

    def stop(self) -> dict[str, Any]:
        with self._state_lock:
            self._cancel_idle_timer()
            backend, self._backend = self._backend, None
            self._running = False
            if backend is not None:
                try:
                    backend.stop()
                except BACKEND_ERRORS as exc:
                    self._set_component(connected=False, detail=str(exc))
                    raise RuntimeError(str(exc)) from exc

        # Wake every viewer so their generators notice the camera is gone.
        with self._frame_condition:
            self._frame = None
            self._frame_seq += 1
            self._frame_condition.notify_all()
        self._wake_viewers()

        self._set_component(connected=False, detail="Camera capture stopped")
        return self._command_response(action="stop", detail="Camera capture stopped")

    def ensure_running(self) -> None:
        """Open the camera if it is not already streaming. Safe to call repeatedly."""
        if not self.enabled:
            raise RuntimeError("Camera is disabled by configuration (CAMERA_ENABLED)")

        with self._state_lock:
            if self._running:
                return

            backend = self._backend_factory(
                width=self.width,
                height=self.height,
                framerate=self.framerate,
                jpeg_quality=self.jpeg_quality,
                encoder=self.encoder,
                buffer_count=self.buffer_count,
            )
            try:
                backend.start(self._publish_frame)
            except BACKEND_ERRORS as exc:
                self._set_component(connected=False, detail=str(exc))
                raise RuntimeError(f"Camera is unavailable: {exc}") from exc

            self.active_encoder = getattr(backend, "active_encoder", None)
            self._backend = backend
            self._running = True
            self._set_component(
                connected=True,
                detail=(
                    f"Capturing {self.width}x{self.height} at {self.framerate} fps "
                    f"({self.active_encoder} JPEG encoder)"
                ),
            )

    def acquire_client_slot(self) -> None:
        """Reserve a viewer slot, starting the camera if it is not running yet.

        Called before the response starts so a full camera or a dead sensor can
        still be answered with a status code instead of a truncated stream.
        Blocks while the camera opens, so async callers run it in a thread.
        """
        # One critical section, so an idle stop cannot slip in between the
        # camera starting and the slot being counted.
        with self._state_lock:
            if self._clients >= self.max_clients:
                raise RuntimeError(
                    f"Camera stream already has {self._clients} of {self.max_clients} viewers"
                )
            self._cancel_idle_timer()
            self.ensure_running()
            self._clients += 1

    def release_client_slot(self) -> None:
        with self._state_lock:
            self._clients = max(0, self._clients - 1)
            if self._clients == 0 and self._running:
                # A short grace period, so a page reload or a quick re-arm
                # does not pay for reopening the sensor.
                self._cancel_idle_timer()
                self._idle_timer = Timer(self.idle_stop_seconds, self._stop_if_idle)
                self._idle_timer.daemon = True
                self._idle_timer.start()

    async def next_frame(self, last_seq: int) -> tuple[int, bytes] | None:
        """Wait on the event loop for a frame newer than ``last_seq``.

        Returns ``None`` once the camera has been stopped, which ends the stream.
        """
        try:
            async with asyncio.timeout(self.frame_timeout_seconds):
                while True:
                    with self._frame_condition:
                        if not self._running:
                            return None
                        if self._frame is not None and self._frame_seq != last_seq:
                            return self._frame_seq, self._frame
                        # Taken under the lock the capture thread publishes
                        # under, so a frame is either seen above or wakes
                        # this event - never lost in between.
                        loop = asyncio.get_running_loop()
                        if self._loop is not loop:
                            self._loop, self._frame_ready = loop, None
                        if self._frame_ready is None:
                            self._frame_ready = asyncio.Event()
                        ready = self._frame_ready
                    await ready.wait()
        except TimeoutError:
            pass
        self._set_component(
            connected=False,
            detail=f"No frame received within {self.frame_timeout_seconds}s",
        )
        raise RuntimeError("Timed out waiting for a camera frame")

    def capture_frame(self) -> bytes:
        """Return the next freshly captured JPEG frame."""
        self.ensure_running()
        with self._frame_condition:
            last_seq = self._frame_seq
            if not self._frame_condition.wait_for(
                lambda: self._frame is not None and self._frame_seq != last_seq,
                timeout=self.frame_timeout_seconds,
            ):
                raise RuntimeError("Timed out waiting for a camera frame")
            return self._frame

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            component = self._component
            running = self._running
            clients = self._clients

        with self._frame_condition:
            frames_captured = self._frames_captured
            last_frame_at = self._last_frame_at

        return {
            "service": "mt0d12-vehicle-api",
            "timestamp": utc_now(),
            "running": running,
            "clients": clients,
            "width": self.width,
            "height": self.height,
            "framerate": self.framerate,
            "jpeg_quality": self.jpeg_quality,
            "encoder": self.active_encoder,
            "frames_captured": frames_captured,
            "last_frame_at": last_frame_at,
            "component": {
                "configured": component.configured,
                "connected": component.connected,
                "detail": component.detail,
                "checked_at": component.checked_at,
            },
        }

    def _publish_frame(self, frame: bytes) -> None:
        with self._frame_condition:
            self._frame = frame
            self._frame_seq += 1
            self._frames_captured += 1
            self._last_frame_at = utc_now()
            self._frame_condition.notify_all()
        self._wake_viewers()

    def _wake_viewers(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._set_frame_ready)
        except RuntimeError:
            # The loop has closed (shutdown, or a test's `asyncio.run` ended);
            # nobody is left waiting on it.
            self._loop = None

    def _set_frame_ready(self) -> None:
        # Swapped for a fresh event, so each waiter sees one set per frame.
        ready, self._frame_ready = self._frame_ready, None
        if ready is not None:
            ready.set()

    def _stop_if_idle(self) -> None:
        with self._state_lock:
            if self._clients or not self._running:
                return
            try:
                self.stop()
            except RuntimeError:
                # stop() already recorded the failure on the component.
                pass

    def _cancel_idle_timer(self) -> None:
        timer, self._idle_timer = self._idle_timer, None
        if timer is not None:
            timer.cancel()

    def _command_response(self, *, action: str, detail: str) -> dict[str, Any]:
        with self._state_lock:
            running = self._running
        return {
            "service": "mt0d12-vehicle-api",
            "action": action,
            "running": running,
            "detail": detail,
            "timestamp": utc_now(),
        }

    def _set_component(self, *, connected: bool, detail: str) -> None:
        with self._state_lock:
            self._component = ComponentSnapshot(
                configured=self.enabled,
                connected=connected,
                detail=detail,
                checked_at=utc_now(),
            )
