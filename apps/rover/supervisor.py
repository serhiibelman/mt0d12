"""
Restarts a task that crashes, so one bug costs that task, not the rover.

Each part of the rover - the motor bus, the flight controller, the probe,
telemetry, the gamepad, the web server - runs as its own supervised task. A
part that raises is logged and started again after a backoff, 1s doubling to
30s, while every other part keeps going: a telemetry bug no longer takes the
driving down with it. A part that returns has finished on purpose (telemetry
with no endpoint configured) and is left finished.

Only `Exception` is caught. Cancellation is how the rover stops a part, and
it goes straight through.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

BACKOFF_MIN_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
# A run this long counts as healthy: the next crash starts the backoff over.
HEALTHY_AFTER_SECONDS = 60.0


async def supervise(
    name: str,
    run: Callable[[], Awaitable[None]],
    *,
    backoff_min: float = BACKOFF_MIN_SECONDS,
    backoff_max: float = BACKOFF_MAX_SECONDS,
    healthy_after: float = HEALTHY_AFTER_SECONDS,
) -> None:
    """Await `run()` until it returns, starting it again whenever it raises."""
    loop = asyncio.get_running_loop()
    failures = 0
    while True:
        started = loop.time()
        try:
            await run()
        except Exception:
            logger.exception("%s crashed", name)
            if asyncio.current_task().cancelling():
                # It was being stopped, and failed on the way out: a stop,
                # not a crash to restart from.
                raise asyncio.CancelledError from None
        else:
            logger.info("%s finished", name)
            return
        if loop.time() - started >= healthy_after:
            failures = 0
        delay = min(backoff_max, backoff_min * 2**failures)
        failures += 1
        logger.warning("Restarting %s in %gs", name, delay)
        await asyncio.sleep(delay)
