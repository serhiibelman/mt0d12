import asyncio
import contextlib
import time

import pytest
import websockets
from fastapi import FastAPI, WebSocket

from apps.rover.rover import ApiServer, Rover
from apps.rover.supervisor import supervise
from conftest import settle

# -- the supervisor ---------------------------------------------------------------


def test_a_part_that_crashes_is_restarted_with_backoff() -> None:
    async def main():
        starts = []

        async def flaky():
            starts.append(time.monotonic())
            raise RuntimeError("bug")

        task = asyncio.create_task(supervise("flaky", flaky, backoff_min=0.02, backoff_max=0.04))
        await settle(lambda: len(starts) >= 4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert gaps[0] >= 0.02 and gaps[1] >= 0.04 and gaps[2] < 0.1  # doubling, capped

    asyncio.run(main())


def test_a_part_that_returns_is_left_finished() -> None:
    async def main():
        runs = []

        async def done_at_once():
            runs.append(1)

        await asyncio.wait_for(supervise("done", done_at_once), timeout=1)
        assert runs == [1]

    asyncio.run(main())


def test_a_long_healthy_run_starts_the_backoff_over() -> None:
    async def main():
        starts = []

        async def crashes_after_a_while():
            starts.append(time.monotonic())
            if len(starts) >= 3:
                await asyncio.sleep(0.05)  # "healthy" by the threshold below
            raise RuntimeError("bug")

        task = asyncio.create_task(
            supervise(
                "part", crashes_after_a_while, backoff_min=0.01, backoff_max=10, healthy_after=0.04
            )
        )
        await settle(lambda: len(starts) >= 4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 0.01, 0.02, then back to 0.01 after the healthy run - not 0.04.
        assert starts[3] - starts[2] < 0.05 + 0.03

    asyncio.run(main())


def test_a_part_that_fails_while_being_stopped_is_not_restarted() -> None:
    async def main():
        runs = []

        async def fails_on_the_way_out():
            runs.append(1)
            try:
                await asyncio.Event().wait()
            finally:
                raise RuntimeError("could not close cleanly")

        task = asyncio.create_task(supervise("part", fails_on_the_way_out, backoff_min=0.01))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.03)

        assert runs == [1]

    asyncio.run(main())


# -- the rover ------------------------------------------------------------------------


class Log(list):
    def part(self, name: str):
        async def run():
            self.append(f"{name} started")
            try:
                await asyncio.Event().wait()
            finally:
                self.append(f"{name} stopped")

        return run


class FakeBus:
    def __init__(self, log: Log) -> None:
        self.log = log
        self.run = log.part("bus")

    async def halt(self) -> None:
        self.log.append("motors stopped")


class FakeCamera:
    def __init__(self, log: Log) -> None:
        self.log = log

    def preload(self) -> None:
        pass

    async def stop(self) -> None:
        self.log.append("camera closed")


class Part:
    def __init__(self, run) -> None:
        self.run = run


class FakeApi:
    def __init__(self, log: Log) -> None:
        self.log = log
        self.run = log.part("web server")

    async def stop(self, task) -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def make_rover(log: Log, **overrides) -> Rover:
    parts = {
        "bus": FakeBus(log),
        "flight_controller": Part(log.part("fc")),
        "status": Part(log.part("probe")),
        "camera": FakeCamera(log),
        "telemetry": Part(log.part("telemetry")),
        "api": FakeApi(log),
        "gamepad": log.part("gamepad"),
    }
    parts.update(overrides)
    return Rover(**parts)


def test_the_rover_stops_the_motors_first_and_closes_the_bus_last() -> None:
    async def main():
        log = Log()
        stopping = asyncio.Event()
        running = asyncio.create_task(make_rover(log).run(stopping))
        await settle(lambda: len(log) == 6)

        stopping.set()
        await asyncio.wait_for(running, timeout=1)

        assert log[6:] == [
            "motors stopped",
            "gamepad stopped",
            "camera closed",
            "web server stopped",
            "telemetry stopped",
            "probe stopped",
            "fc stopped",
            "bus stopped",
        ]

    asyncio.run(main())


def test_a_crashing_part_costs_only_itself() -> None:
    async def main():
        log = Log()
        crashes = []

        async def telemetry():
            crashes.append(1)
            if len(crashes) == 1:
                raise RuntimeError("a bug in telemetry")
            await log.part("telemetry")()

        rover = make_rover(log, telemetry=Part(telemetry))
        stopping = asyncio.Event()
        running = asyncio.create_task(rover.run(stopping))
        # The supervisor's first retry is a second away.
        await settle(lambda: "telemetry started" in log, timeout=3)

        assert len(crashes) == 2
        assert not any("stopped" in entry for entry in log), "nothing else was touched"
        stopping.set()
        await asyncio.wait_for(running, timeout=1)

    asyncio.run(main())


# -- the web server as a task ---------------------------------------------------


def test_the_web_server_stops_promptly_with_a_status_page_open() -> None:
    # uvicorn 0.22 on Python 3.12+ waits for open connections before it asks
    # them to close, so a status page left open held SIGTERM up forever.
    app = FastAPI()
    connected = asyncio.Event()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        connected.set()
        while True:
            await websocket.receive_text()

    async def main():
        api = ApiServer(app, host="127.0.0.1", port=0, shutdown_timeout=3.0)
        task = asyncio.create_task(api.run())
        await settle(lambda: api._server is not None and api._server.started)
        port = api._server.servers[0].sockets[0].getsockname()[1]

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", close_timeout=1) as client:
            await asyncio.wait_for(connected.wait(), timeout=1)
            started = time.monotonic()
            await api.stop(task)
            took = time.monotonic() - started
            # The page is told the server is going, with 1012 "restarting".
            with pytest.raises(websockets.ConnectionClosed) as closed:
                await asyncio.wait_for(client.recv(), timeout=2)

        assert task.done() and not task.cancelled(), "it exited, rather than being cancelled"
        assert took < 1.0
        assert closed.value.rcvd.code == 1012

    asyncio.run(main())


def test_a_web_server_that_cannot_bind_is_a_crash_to_retry() -> None:
    async def main():
        holder = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = holder.sockets[0].getsockname()[1]
        try:
            api = ApiServer(FastAPI(), host="127.0.0.1", port=port)
            with pytest.raises(RuntimeError, match=f"could not start on port {port}"):
                await api.run()
        finally:
            holder.close()

    asyncio.run(main())
