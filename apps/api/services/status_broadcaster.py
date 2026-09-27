"""One producer for /ws/status, fanned out to every viewer.

Each viewer used to take and serialise its own snapshot, so N viewers meant N
copies of the same work every tick. Here one task renders the snapshot once per
tick and hands the same text to every viewer through a queue of their own.

The queues hold one message. A viewer that has not taken the last update when
the next one arrives gets the new one in its place - newest wins, the rule
`CameraService` applies to frames - so the producer never waits on anyone and a
slow viewer only ever falls behind itself.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

# 5 Hz: live enough to watch the rover tilt, and on a Pi 1 each tick is one
# snapshot copy and one JSON render however many viewers there are.
STATUS_STREAM_INTERVAL = 0.2


class StatusBroadcaster:
    def __init__(self, render: Callable[[], str], interval: float = STATUS_STREAM_INTERVAL) -> None:
        self._render = render
        self._interval = interval
        self._queues: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task[None] | None = None

    @property
    def viewers(self) -> int:
        return len(self._queues)

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[str]]:
        """A queue that receives every update while the `with` block runs.

        The producer runs only while someone is watching: the first viewer
        starts it and the last one to leave cancels it, the way the camera
        opens on the first stream and not at boot.
        """
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._queues.add(queue)
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="status-broadcaster")
        try:
            yield queue
        finally:
            self._queues.discard(queue)
            if not self._queues and self._task is not None:
                # No await needed: the producer is parked in sleep(), and
                # cancel() makes that sleep raise the next time the loop runs.
                self._task.cancel()
                self._task = None

    async def stop(self) -> None:
        """Cancel the producer and wait until it has finished. For shutdown."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                message = self._render()
            except Exception:
                # One bad snapshot must not end the stream for everyone.
                logger.exception("Could not render the status snapshot")
            else:
                # No await inside the loop, so no viewer can join or leave
                # while it walks the set.
                for queue in self._queues:
                    _put_newest(queue, message)
            await asyncio.sleep(self._interval)


def _put_newest(queue: asyncio.Queue[str], message: str) -> None:
    if queue.full():
        queue.get_nowait()  # the viewer never took it, and it is stale now
    queue.put_nowait(message)
