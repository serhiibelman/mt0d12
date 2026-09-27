import asyncio

from apps.api.services.status_broadcaster import StatusBroadcaster

# Short enough that a test sits through a few ticks in milliseconds.
TICK = 0.005


class CountingRender:
    """Renders "1", "2", ... so a test can tell one tick from the next."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return str(self.calls)


def test_every_viewer_gets_the_same_render() -> None:
    async def scenario() -> None:
        render = CountingRender()
        broadcaster = StatusBroadcaster(render, TICK)
        with broadcaster.subscribe() as first, broadcaster.subscribe() as second:
            assert await first.get() == await second.get() == "1"
            # One render served both viewers.
            assert render.calls == 1

    asyncio.run(scenario())


def test_a_slow_viewer_only_holds_the_newest_update() -> None:
    async def scenario() -> None:
        render = CountingRender()
        broadcaster = StatusBroadcaster(render, TICK)
        with broadcaster.subscribe() as updates:
            await asyncio.sleep(TICK * 10)  # a viewer that takes nothing for a while
            assert updates.qsize() == 1
            assert await updates.get() == str(render.calls)

    asyncio.run(scenario())


def test_the_producer_runs_only_while_someone_watches() -> None:
    async def scenario() -> None:
        render = CountingRender()
        broadcaster = StatusBroadcaster(render, TICK)
        with broadcaster.subscribe() as updates:
            await updates.get()
        assert broadcaster.viewers == 0

        calls_when_left = render.calls
        await asyncio.sleep(TICK * 5)
        assert render.calls == calls_when_left

        # The next viewer starts it again.
        with broadcaster.subscribe() as updates:
            assert await updates.get() == str(calls_when_left + 1)

    asyncio.run(scenario())


def test_a_failed_render_does_not_end_the_stream() -> None:
    async def scenario() -> None:
        outcomes = iter([RuntimeError("probe hiccup")])

        def render() -> str:
            if (error := next(outcomes, None)) is not None:
                raise error
            return "ok"

        broadcaster = StatusBroadcaster(render, TICK)
        with broadcaster.subscribe() as updates:
            assert await asyncio.wait_for(updates.get(), timeout=1) == "ok"

    asyncio.run(scenario())


def test_stop_cancels_a_running_producer() -> None:
    async def scenario() -> None:
        render = CountingRender()
        broadcaster = StatusBroadcaster(render, TICK)
        with broadcaster.subscribe() as updates:
            await updates.get()
            await broadcaster.stop()
            calls_when_stopped = render.calls
            await asyncio.sleep(TICK * 5)
            assert render.calls == calls_when_stopped

    asyncio.run(scenario())
