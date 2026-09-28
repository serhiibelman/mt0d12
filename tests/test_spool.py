"""Tests for the durable outbox in `lib/spool`.

Everything here runs against a temporary file or `:memory:`, so no test
touches the spool the vehicle actually uses.
"""

import asyncio
import functools
import sqlite3

import pytest

from lib.spool import Spool
from lib.spool.spool import SCHEMA_VERSION

TOPIC = "rover/rover-01/telemetry"


def asynctest(test):
    """Run an `async def` test on its own event loop; fixtures pass through."""

    @functools.wraps(test)
    def run(*args, **kwargs):
        asyncio.run(test(*args, **kwargs))

    return run


@pytest.fixture()
def spool():
    instance = Spool(":memory:")
    yield instance
    asyncio.run(instance.close())


@asynctest
async def test_messages_come_back_in_the_order_they_were_queued(spool):
    # Replay order is the whole point: a backlog has to read like a live feed.
    for index in range(3):
        await spool.append(TOPIC, f'{{"n":{index}}}')

    assert [message.payload for message in await spool.pending(10)] == [
        '{"n":0}',
        '{"n":1}',
        '{"n":2}',
    ]


@asynctest
async def test_pending_is_capped_by_the_limit(spool):
    for index in range(5):
        await spool.append(TOPIC, f'{{"n":{index}}}')

    assert len(await spool.pending(2)) == 2


@asynctest
async def test_the_topic_travels_with_the_message(spool):
    await spool.append("rover/rover-09/telemetry", "{}")

    assert (await spool.pending(1))[0].topic == "rover/rover-09/telemetry"


@asynctest
async def test_discard_removes_only_what_was_delivered(spool):
    first = await spool.append(TOPIC, '{"n":0}')
    await spool.append(TOPIC, '{"n":1}')

    await spool.discard([first])

    assert [message.payload for message in await spool.pending(10)] == ['{"n":1}']


@asynctest
async def test_discarding_nothing_is_allowed(spool):
    await spool.append(TOPIC, "{}")

    await spool.discard([])

    assert await spool.depth() == 1


@asynctest
async def test_depth_counts_what_is_still_waiting(spool):
    assert await spool.depth() == 0
    await spool.append(TOPIC, "{}")
    assert await spool.depth() == 1


@asynctest
async def test_the_row_cap_drops_the_oldest_and_keeps_the_newest():
    # A full SD card takes the vehicle down, so the spool is bounded. During a
    # long outage the recent samples are the ones that explain what happened.
    spool = Spool(":memory:", max_rows=3)

    for index in range(5):
        await spool.append(TOPIC, f'{{"n":{index}}}')

    assert [message.payload for message in await spool.pending(10)] == [
        '{"n":2}',
        '{"n":3}',
        '{"n":4}',
    ]
    assert spool.dropped == 2
    await spool.close()


@asynctest
async def test_messages_older_than_the_age_cap_are_dropped():
    clock = {"t": 0.0}
    spool = Spool(":memory:", max_age_seconds=100, time_func=lambda: clock["t"])
    await spool.append(TOPIC, '{"n":"old"}')

    clock["t"] = 101.0
    await spool.append(TOPIC, '{"n":"new"}')

    assert [message.payload for message in await spool.pending(10)] == ['{"n":"new"}']
    assert spool.dropped == 1
    await spool.close()


@asynctest
async def test_zero_caps_switch_retention_off():
    spool = Spool(":memory:", max_rows=0, max_age_seconds=0)

    for index in range(50):
        await spool.append(TOPIC, "{}")

    assert await spool.depth() == 50
    assert spool.dropped == 0
    await spool.close()


@asynctest
async def test_a_queued_message_survives_the_process(tmp_path):
    # The reason this is SQLite and not a list: losing power mid-drive is
    # exactly when the uplink is down and the samples matter.
    path = tmp_path / "spool.sqlite3"
    first = Spool(path)
    await first.append(TOPIC, '{"n":"before"}')
    await first.close()

    second = Spool(path)

    assert [message.payload for message in await second.pending(10)] == ['{"n":"before"}']
    await second.close()


@asynctest
async def test_the_parent_directory_is_created(tmp_path):
    path = tmp_path / "var" / "nested" / "spool.sqlite3"

    spool = Spool(path)
    await spool.append(TOPIC, "{}")

    assert path.exists()
    await spool.close()


@asynctest
async def test_a_fresh_spool_is_stamped_with_the_schema_version(tmp_path):
    path = tmp_path / "spool.sqlite3"
    spool = Spool(path)
    await spool.append(TOPIC, "{}")
    await spool.close()

    stored = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
    assert stored == SCHEMA_VERSION


@asynctest
async def test_a_spool_from_another_schema_version_is_discarded(tmp_path):
    # `CREATE TABLE IF NOT EXISTS` would silently keep the old table and fail
    # on the next insert instead - on a rover, at the worst possible moment.
    path = tmp_path / "spool.sqlite3"
    first = Spool(path)
    await first.append(TOPIC, '{"n":"stale"}')
    await first.close()
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()

    second = Spool(path)

    assert await second.depth() == 0
    # Still usable afterwards, and re-stamped with the version we understand.
    await second.append(TOPIC, '{"n":"fresh"}')
    assert await second.depth() == 1
    await second.close()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
