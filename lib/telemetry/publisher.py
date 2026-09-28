from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Awaitable, Callable, Protocol

from lib.spool import Spool
from lib.telemetry.config import TelemetryConfig

logger = logging.getLogger(__name__)

NOT_CONFIGURED = "AWS IoT is not configured (IOT_ENDPOINT is empty)"
# At-least-once: a dropped uplink costs a duplicate row, not a lost sample.
QOS_AT_LEAST_ONCE = 1
# A failed drain retries after 1s, doubling to 60s: an outage costs one
# attempt a minute, not one every tick, and a link that comes back is used
# within a minute. New samples keep spooling meanwhile.
RETRY_MIN_SECONDS = 1.0
RETRY_MAX_SECONDS = 60.0


# Values that change on every sample by definition, so they cannot count as
# news. The clocks are obvious. `yaw_deg` is here because a compass drifts on
# its own: left in, a parked rover would publish every few seconds and undo the
# idle heartbeat. Roll and pitch are gravity-referenced and stay put, so a
# vehicle that tips over still says so immediately.
#
# The Pi's own gauges are here for the same reason: temperature, load, memory
# and disk move on every reading. What counts instead is `pi.warnings` and the
# throttle flags, so a brownout or a card filling up is still heard at once.
VOLATILE_KEYS = (
    "timestamp",
    "checked_at",
    "last_frame_at",
    "recorded_at",
    "yaw_deg",
    "cpu_temp_c",
    "load_1m",
    "memory_available_mb",
    "memory_available_percent",
    "disk_free_mb",
    "disk_free_percent",
)


def significant(value: Any) -> Any:
    """The snapshot with its clocks removed - what "unchanged" is judged on."""
    if isinstance(value, dict):
        return {k: significant(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [significant(item) for item in value]
    return value


class Connection(Protocol):
    """The slice of an MQTT client this module actually uses."""

    def connect(self) -> Awaitable[Any]: ...

    def publish(self, topic: str, payload: str, qos: int) -> Awaitable[Any]: ...

    def disconnect(self) -> Awaitable[Any]: ...


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class _AiomqttConnection:
    """Adapts aiomqtt to the three calls the publisher makes.

    AWS IoT Core is plain MQTT over TLS 1.2 with a client certificate, so the
    pure-Python client talks to it directly - which matters on the Pi 1, where
    the C extension behind `awsiotsdk` has no 32-bit ARM wheel. aiomqtt is
    paho on the event loop: no network thread of its own, and a QoS 1 publish
    returns once the broker has acknowledged it.
    """

    def __init__(self, config: TelemetryConfig) -> None:
        self.config = config
        self._client: Any = None

    async def connect(self) -> None:
        import ssl

        import aiomqtt

        client = aiomqtt.Client(
            self.config.endpoint,
            self.config.port,
            identifier=self.config.client_id or self.config.thing_name,
            protocol=aiomqtt.ProtocolVersion.V311,
            clean_session=False,
            keepalive=self.config.keep_alive_seconds,
            timeout=self.config.publish_timeout_seconds,
            tls_params=aiomqtt.TLSParameters(
                ca_certs=self.config.root_ca_path or None,
                certfile=self.config.cert_path,
                keyfile=self.config.key_path,
                tls_version=ssl.PROTOCOL_TLSv1_2,
            ),
        )
        # The context manager's two halves, held apart: the connection lives
        # across many publishes, not one `async with` block.
        await client.__aenter__()
        self._client = client

    async def publish(self, topic: str, payload: str, qos: int) -> None:
        await self._client.publish(
            topic, payload, qos=qos, timeout=self.config.publish_timeout_seconds
        )

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.__aexit__(None, None, None)


def _build_connection(config: TelemetryConfig) -> Connection:
    return _AiomqttConnection(config)


def _build_spool(config: TelemetryConfig) -> Spool | None:
    if not config.spool_enabled:
        return None
    return Spool(
        config.spool_path,
        max_rows=config.spool_max_rows,
        max_age_seconds=config.spool_max_age_seconds,
    )


class TelemetryPublisher:
    """
    Publishes vehicle snapshots to AWS IoT Core from the event loop.

    Nothing here is on the driving path: a message is written to the local
    spool first and only then sent, so a failed uplink delays telemetry rather
    than losing it, and with no endpoint configured the whole thing is a no-op
    so the vehicle runs exactly as it did before.

    Two tasks. The sampler takes a snapshot every interval and, when one is
    due, spools it - a local write, so it never waits on the network. The
    drainer sends the spool oldest first whenever the sampler wakes it, and on
    a failure backs off (1s doubling to 60s) while the sampler carries on. So
    a long backlog drains without holding up new samples, and an outage costs
    an attempt a minute rather than one a tick. Every network call is under
    `asyncio.timeout`, so a half-open connection costs a timeout, not a hang.
    """

    def __init__(
        self,
        snapshot: Callable[[], dict[str, Any]],
        config: TelemetryConfig | None = None,
        connection_factory: Callable[[TelemetryConfig], Connection] = _build_connection,
        time_func: Callable[[], float] = monotonic,
        spool_factory: Callable[[TelemetryConfig], Spool | None] = _build_spool,
        retry_min_seconds: float = RETRY_MIN_SECONDS,
        retry_max_seconds: float = RETRY_MAX_SECONDS,
    ) -> None:
        self.snapshot = snapshot
        self.config = config or TelemetryConfig()
        self._connection_factory = connection_factory
        self._now = time_func
        self._spool_factory = spool_factory
        self._retry_min = retry_min_seconds
        self._retry_max = retry_max_seconds
        self._connection: Connection | None = None
        self._spool: Spool | None = None
        self._spool_built = False
        # Connecting is serialised: two sends finding no connection would
        # otherwise open two, and one would leak.
        self._connect_lock = asyncio.Lock()
        # Draining is serialised separately: two drains reading the same batch
        # would send every message in it twice.
        self._flush_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.published = 0
        self.failed = 0
        self.skipped = 0
        self.spooled = 0
        self._last_signature: str | None = None
        self._last_published_at: float | None = None

    @property
    def configured(self) -> bool:
        return bool(self.config.endpoint and self.config.cert_path and self.config.key_path)

    async def start(self) -> None:
        if not self.configured:
            logger.info("Telemetry publisher disabled: %s", NOT_CONFIGURED)
            return
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._sample_loop(), name="telemetry-sampler"),
            asyncio.create_task(self._drain_loop(), name="telemetry-drainer"),
        ]

    async def stop(self) -> None:
        """
        Stop both tasks, then disconnect and close the spool.

        Loses nothing: every sample the sampler accepted is committed to the
        spool before it counts as recorded, and a drain cut off mid-batch
        still forgets the messages the broker had acknowledged (see `flush`),
        so what is left on disk is exactly what has not gone out - and it goes
        out on the next start. No final drain is attempted: with the uplink
        down that would hold up shutdown for a timeout, to deliver nothing.
        """
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._disconnect()
        if self._spool is not None:
            await self._spool.close()

    def message(self, snapshot: dict[str, Any], trigger: str = "change") -> dict[str, Any]:
        """
        The payload. `thing_name` travels in the body so the Lambda does not
        have to parse it out of the topic.
        """
        return {
            "thing_name": self.config.thing_name,
            "recorded_at": datetime.now(timezone.utc),
            "trigger": trigger,
            "snapshot": snapshot,
        }

    async def publish_once(
        self, snapshot: dict[str, Any] | None = None, trigger: str = "change"
    ) -> bool:
        """
        Record one sample and drain the spool. Returns whether the uplink
        took it; never raises.

        With a spool, False means "kept for later" rather than "lost" - the
        sample is on disk and goes out with the rest of the backlog once the
        link is back.
        """
        if not self.configured:
            return False
        snapshot = self.snapshot() if snapshot is None else snapshot
        sent = await self._record(snapshot, trigger)
        return await self.flush() if sent is None else sent

    async def publish_if_due(self) -> bool:
        """
        Publish only when something changed, or the heartbeat came due.

        The background tasks do this in two halves - the sampler records, the
        drainer sends - so that a slow uplink never delays a sample. This is
        both halves in one call, for a caller that wants the answer.
        """
        due = self._due()
        if due is None:
            return False
        snapshot, trigger = due
        return await self.publish_once(snapshot, trigger)

    async def flush(self) -> bool:
        """Send spooled messages oldest first. True if any went out."""
        delivered, _complete = await self._drain()
        return delivered > 0

    async def spool_depth(self) -> int:
        """Messages waiting for the uplink. 0 when spooling is switched off."""
        spool = self._get_spool()
        return 0 if spool is None else await spool.depth()

    # -- the two tasks -----------------------------------------------------

    async def _sample_loop(self) -> None:
        while True:
            try:
                due = self._due()
                if due is not None:
                    await self._record(*due)
            except Exception:
                # A bad snapshot costs that sample, not the publisher.
                logger.exception("Telemetry sample failed")
            # A skipped tick still owes the backlog an attempt: without this a
            # parked rover would sit on spooled messages until it moved again.
            self._wake.set()
            await asyncio.sleep(self.config.interval_seconds)

    async def _drain_loop(self) -> None:
        failures = 0
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                _delivered, complete = await self._drain()
            except Exception:
                logger.exception("Telemetry drain failed")
                complete = False
            if complete:
                failures = 0
                continue
            delay = min(self._retry_max, self._retry_min * 2**failures)
            failures += 1
            logger.info("Telemetry uplink unavailable; retrying in %gs", delay)
            # Wakes that arrive meanwhile are kept, so the retry runs straight
            # after the sleep.
            await asyncio.sleep(delay)
            self._wake.set()

    # -- recording ---------------------------------------------------------

    def _due(self) -> tuple[dict[str, Any], str] | None:
        """
        The snapshot and it's trigger when a sample is due, else None.

        A parked rover produces thousands of identical snapshots a day; storing
        them costs disk and tells nobody anything. The heartbeat is what keeps
        silence meaningful: no message for more than one interval means the
        vehicle is gone, not idle.
        """
        if not self.configured:
            return None

        snapshot = self.snapshot()
        signature = json.dumps(significant(snapshot), sort_keys=True, default=_json_default)

        if signature != self._last_signature:
            return snapshot, "change"

        if self._due_for_heartbeat(snapshot):
            return snapshot, "idle" if self._is_idle(snapshot) else "heartbeat"

        self.skipped += 1
        return None

    async def _record(self, snapshot: dict[str, Any], trigger: str) -> bool | None:
        """Spool one sample (None: it goes out with the next drain), or with
        no usable spool send it now and say whether it went (publish or drop,
        as before the spool existed)."""
        payload = json.dumps(self.message(snapshot, trigger), default=_json_default)
        if await self._enqueue(payload):
            # Ours now, whatever the uplink does: the sample counts as
            # recorded, so an outage does not re-queue it on every tick.
            self._remember(snapshot)
            return None
        sent = await self._send(self.config.resolved_topic, payload)
        if sent:
            self._remember(snapshot)
        return sent

    async def _enqueue(self, payload: str) -> bool:
        """Write one message to the spool. False if there is no usable spool."""
        spool = self._get_spool()
        if spool is None:
            return False
        try:
            await spool.append(self.config.resolved_topic, payload)
        except Exception as error:
            # A read-only card or a corrupt file must not stop telemetry: drop
            # back to publishing straight through and say so once.
            logger.warning("Telemetry spool unusable, publishing direct", exc_info=error)
            self._spool = None
            return False
        self.spooled += 1
        return True

    def _remember(self, snapshot: dict[str, Any]) -> None:
        """Mark this sample as recorded - what change detection compares to."""
        self._last_signature = json.dumps(
            significant(snapshot), sort_keys=True, default=_json_default
        )
        self._last_published_at = self._now()

    def _get_spool(self) -> Spool | None:
        """Built on first use, so constructing a publisher opens no file."""
        if not self._spool_built:
            self._spool_built = True
            self._spool = self._spool_factory(self.config)
        return self._spool

    @staticmethod
    def at_rest(snapshot: dict[str, Any]) -> bool:
        """True when no motor has been commanded to turn.

        `motor_feedback` carries the commanded rpm, so this says "nobody asked
        it to move" rather than "it is not moving" - close enough to decide how
        chatty to be, and it never mistakes sensor noise for motion.
        """
        feedback = snapshot.get("motor_feedback") or []
        return all(not entry.get("rpm") for entry in feedback)

    def _is_idle(self, snapshot: dict[str, Any]) -> bool:
        """At rest *and* configured to treat that differently."""
        return self.config.idle_heartbeat_seconds > 0 and self.at_rest(snapshot)

    def _heartbeat_interval(self, snapshot: dict[str, Any]) -> float:
        if self._is_idle(snapshot):
            return self.config.idle_heartbeat_seconds
        return self.config.heartbeat_seconds

    def _due_for_heartbeat(self, snapshot: dict[str, Any]) -> bool:
        interval = self._heartbeat_interval(snapshot)
        if interval <= 0:
            return False
        if self._last_published_at is None:
            return True
        return self._now() - self._last_published_at >= interval

    # -- sending -----------------------------------------------------------

    async def _drain(self) -> tuple[int, bool]:
        """Send the spool oldest first, stopping at the first failure.

        Returns how many went out and whether the spool was emptied. Order is
        the order they were recorded, so a replayed backlog reads the same as
        a live one - `recorded_at` travels in the payload, so a late message
        is still stamped with when it happened. Samples spooled while this
        runs are picked up by its next batch.
        """
        spool = self._get_spool()
        if spool is None:
            return 0, True
        delivered = 0
        async with self._flush_lock:
            while True:
                batch = await spool.pending(self.config.spool_batch)
                if not batch:
                    return delivered, True
                sent_ids: list[int] = []
                try:
                    for queued in batch:
                        if not await self._send(queued.topic, queued.payload):
                            break
                        sent_ids.append(queued.id)
                finally:
                    # Acknowledged means delivered, even when a stop cancels
                    # the drain mid-batch: forget those, or they go out twice
                    # on the next start. Shielded so the cancel cannot cut
                    # the commit short.
                    await asyncio.shield(spool.discard(sent_ids))
                delivered += len(sent_ids)
                if len(sent_ids) < len(batch):
                    return delivered, False

    async def _send(self, topic: str, payload: str) -> bool:
        """One publish attempt against the uplink. Never raises."""
        try:
            connection = await self._ensure_connection()
            async with asyncio.timeout(self.config.publish_timeout_seconds):
                await connection.publish(topic=topic, payload=payload, qos=QOS_AT_LEAST_ONCE)
        except Exception as error:  # the uplink is allowed to fail
            self.failed += 1
            logger.warning("Telemetry publish failed: %s", error or type(error).__name__)
            await self._disconnect()
            return False
        self.published += 1
        return True

    async def _ensure_connection(self) -> Connection:
        async with self._connect_lock:
            if self._connection is None:
                connection = self._connection_factory(self.config)
                try:
                    async with asyncio.timeout(self.config.publish_timeout_seconds):
                        await connection.connect()
                except BaseException:
                    # A half-made connection is released, not leaked.
                    await self._close_quietly(connection)
                    raise
                self._connection = connection
            return self._connection

    async def _disconnect(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await self._close_quietly(connection)

    async def _close_quietly(self, connection: Connection) -> None:
        try:
            async with asyncio.timeout(self.config.publish_timeout_seconds):
                await connection.disconnect()
        except Exception as error:
            logger.debug("Telemetry disconnect failed", exc_info=error)
