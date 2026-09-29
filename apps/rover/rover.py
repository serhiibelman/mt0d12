"""
The whole rover as one asyncio program.

One process owns the motor bus, the flight controller link, the camera, the
probe, telemetry, the gamepad link and the web server, each a task on one
event loop under `supervise`. They share one `MotorBus`, so the status page,
the gamepad and the /motors ramps take turns on the same open port instead of
fighting over it, and telemetry runs whichever of them is driving.

Stopping is ordered, so the rover is never left moving while the parts that
would stop it are already gone:

1. The motors stop, and the bus refuses every claim after - before anything
   else, so nothing below can be what keeps them turning.
2. The gamepad link goes.
3. The camera closes, which ends every MJPEG stream.
4. The web server closes its connections - status pages first, so no one is
   left waiting on them - and exits.
5. Telemetry, the probe and the flight controller stop. Telemetry loses
   nothing: its spool holds what has not gone out.
6. The motor bus closes its port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import uvicorn

from apps.rover.supervisor import supervise

logger = logging.getLogger(__name__)

# How long the web server may take to close before it is cancelled.
API_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class Part(Protocol):
    async def run(self) -> None: ...


class _Server(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        # The rover takes SIGINT and SIGTERM itself, to stop in its own order.
        pass


class ApiServer:
    """uvicorn, as one task of the rover rather than the process around it."""

    def __init__(
        self,
        app: Any,
        *,
        host: str,
        port: int,
        shutdown_timeout: float = API_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.shutdown_timeout = shutdown_timeout
        self._server: _Server | None = None
        self._stopping = False

    async def run(self) -> None:
        # A new server each run: one that has exited cannot be started again.
        self._server = server = _Server(
            uvicorn.Config(self.app, host=self.host, port=self.port, lifespan="on")
        )
        try:
            await server.serve()
        except SystemExit as exc:
            # What uvicorn does when it cannot bind - the port still held by
            # a process on its way out. A crash, so the supervisor retries.
            raise RuntimeError(f"The web server could not start on port {self.port}") from exc
        if not self._stopping:
            raise RuntimeError("The web server exited on its own")

    async def stop(self, task: asyncio.Task[None]) -> None:
        """Close every connection, then let the server exit; cancel it if it
        has not within `shutdown_timeout`."""
        self._stopping = True
        server = self._server
        if server is None or not server.started:
            # Not serving - between restarts, or still starting.
            await _cancel(task)
            return
        # uvicorn 0.22 on Python 3.12+ waits for open connections to close
        # before it asks them to, so an open status page held SIGTERM up
        # forever. Asking them first is the fix.
        for connection in list(server.server_state.connections):
            connection.shutdown()
        server.should_exit = True
        done, _ = await asyncio.wait([task], timeout=self.shutdown_timeout)
        if not done:
            logger.warning("Web server still closing after %gs; cancelling", self.shutdown_timeout)
            await _cancel(task)


class Rover:
    def __init__(
        self,
        *,
        bus: Any,
        flight_controller: Part,
        status: Part,
        camera: Any,
        telemetry: Part,
        api: ApiServer,
        gamepad: Callable[[], Awaitable[None]],
    ) -> None:
        self.bus = bus
        self.flight_controller = flight_controller
        self.status = status
        self.camera = camera
        self.telemetry = telemetry
        self.api = api
        self.gamepad = gamepad

    async def run(self, stopping: asyncio.Event) -> None:
        """Run every part until `stopping` is set, then stop them in order."""
        # In the order they are needed: the bus and the readings first, so the
        # first status page to connect already has something to show.
        parts: dict[str, Callable[[], Awaitable[None]]] = {
            "motor bus": self.bus.run,
            "flight controller": self.flight_controller.run,
            "status probe": self.status.run,
            "telemetry": self.telemetry.run,
            "gamepad": self.gamepad,
            "web server": self.api.run,
        }
        tasks = {
            name: asyncio.create_task(supervise(name, run), name=name)
            for name, run in parts.items()
        }
        # The camera opens on the first viewer, not now, so the sensor stays
        # powered down while nobody watches. Its library loads now, as that is
        # the slow part: seconds of the Pi 1's only core.
        preload = asyncio.create_task(asyncio.to_thread(self.camera.preload), name="camera preload")
        try:
            await stopping.wait()
        finally:
            await self._stop(tasks)
            await preload

    async def _stop(self, tasks: dict[str, asyncio.Task[None]]) -> None:
        logger.info("Stopping: motors first")
        await self.bus.halt()
        await _cancel(tasks["gamepad"])
        try:
            await self.camera.stop()
        except RuntimeError as exc:
            logger.warning("Camera did not close cleanly: %s", exc)
        await self.api.stop(tasks["web server"])
        for name in ("telemetry", "status probe", "flight controller", "motor bus"):
            await _cancel(tasks[name])
        logger.info("Stopped")


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
