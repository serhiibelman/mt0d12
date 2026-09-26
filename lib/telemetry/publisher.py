from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Any, Callable, Protocol

from lib.spool import Spool
from lib.telemetry.config import TelemetryConfig

logger = logging.getLogger(__name__)

NOT_CONFIGURED = "AWS IoT is not configured (IOT_ENDPOINT is empty)"
# At-least-once: a dropped uplink costs a duplicate row, not a lost sample.
QOS_AT_LEAST_ONCE = 1


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

    def connect(self) -> Any: ...

    def publish(self, topic: str, payload: str, qos: int) -> Any: ...

    def disconnect(self) -> Any: ...


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class _PahoConnection:
    """Adapts paho-mqtt to the three calls the publisher makes.

    AWS IoT Core is plain MQTT over TLS 1.2 with a client certificate, so the
    pure-Python paho client talks to it directly - which matters on the Pi 1,
    where the C extension behind `awsiotsdk` has no 32-bit ARM wheel.
    """

    def __init__(self, config: TelemetryConfig) -> None:
        self.config = config
        self._client: Any = None

    def connect(self) -> None:
        import ssl

        import paho.mqtt.client as mqtt

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.config.client_id or self.config.thing_name,
            protocol=mqtt.MQTTv311,
            clean_session=False,
        )
        client.tls_set(
            ca_certs=self.config.root_ca_path or None,
            certfile=self.config.cert_path,
            keyfile=self.config.key_path,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )
        client.connect(
            self.config.endpoint,
            self.config.port,
            keepalive=self.config.keep_alive_seconds,
        )
        # paho does its network I/O on its own thread; without loop_start the
        # QoS 1 handshake never completes and publishes just queue up.
        client.loop_start()
        self._client = client

    def publish(self, topic: str, payload: str, qos: int) -> None:
        import paho.mqtt.client as mqtt

        info = self._client.publish(topic, payload, qos=qos)
        info.wait_for_publish(timeout=self.config.publish_timeout_seconds)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed (rc={info.rc})")

    def disconnect(self) -> None:
        if self._client is None:
            return
        self._client.loop_stop()
        self._client.disconnect()
        self._client = None


def _build_connection(config: TelemetryConfig) -> Connection:
    return _PahoConnection(config)


def _build_spool(config: TelemetryConfig) -> Spool | None:
    if not config.spool_enabled:
        return None
    return Spool(
        config.spool_path,
        max_rows=config.spool_max_rows,
        max_age_seconds=config.spool_max_age_seconds,
    )


class TelemetryPublisher:
    """Publishes vehicle snapshots to AWS IoT Core on a background thread.

    Nothing here is on the driving path: a message is written to the local
    spool first and only then sent, so a failed uplink delays telemetry rather
    than losing it, the next tick reconnects and drains the backlog oldest
    first, and with no endpoint configured the whole thing is a no-op so the
    vehicle runs exactly as it did before.
    """

    def __init__(
        self,
        snapshot: Callable[[], dict[str, Any]],
        config: TelemetryConfig | None = None,
        connection_factory: Callable[[TelemetryConfig], Connection] = _build_connection,
        sleep_func: Callable[[float], None] = sleep,
        time_func: Callable[[], float] = monotonic,
        spool_factory: Callable[[TelemetryConfig], Spool | None] = _build_spool,
    ) -> None:
        self.snapshot = snapshot
        self.config = config or TelemetryConfig()
        self._connection_factory = connection_factory
        self._sleep = sleep_func
        self._now = time_func
        self._spool_factory = spool_factory
        self._connection: Connection | None = None
        self._spool: Spool | None = None
        self._spool_built = False
        self._stop_event = Event()
        self._lock = Lock()
        # Draining is serialised separately from the connection: two threads
        # reading the same batch would send every message in it twice.
        self._flush_lock = Lock()
        self._thread: Thread | None = None
        self.published = 0
        self.failed = 0
        self.skipped = 0
        self.spooled = 0
        self._last_signature: str | None = None
        self._last_published_at: float | None = None

    @property
    def configured(self) -> bool:
        return bool(self.config.endpoint and self.config.cert_path and self.config.key_path)

    def start(self) -> None:
        if not self.configured:
            logger.info("Telemetry publisher disabled: %s", NOT_CONFIGURED)
            return
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._loop, name="telemetry-publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.config.interval_seconds + 1)
            self._thread = None
        self._disconnect()
        if self._spool is not None:
            # Whatever is left stays on disk and goes out on the next start.
            self._spool.close()

    def message(self, snapshot: dict[str, Any], trigger: str = "change") -> dict[str, Any]:
        """The payload. `thing_name` travels in the body so the Lambda does not
        have to parse it out of the topic."""
        return {
            "thing_name": self.config.thing_name,
            "recorded_at": datetime.now(timezone.utc),
            "trigger": trigger,
            "snapshot": snapshot,
        }

    def publish_once(self, snapshot: dict[str, Any] | None = None, trigger: str = "change") -> bool:
        """Queue one sample and drain the spool. Returns whether the uplink
        took it; never raises.

        With a spool, False means "kept for later" rather than "lost" - the
        sample is on disk and goes out with the rest of the backlog once the
        link is back.
        """
        if not self.configured:
            return False
        snapshot = self.snapshot() if snapshot is None else snapshot
        payload = json.dumps(self.message(snapshot, trigger), default=_json_default)

        if self._enqueue(payload):
            # Ours now, whatever the uplink does: the sample counts as
            # recorded, so an outage does not re-queue it on every tick.
            self._remember(snapshot)
            return self.flush()

        # No spool (or it could not be written): publish or drop, as before.
        sent = self._send(self.config.resolved_topic, payload)
        if sent:
            self._remember(snapshot)
        return sent

    def flush(self) -> bool:
        """Send spooled messages oldest first, stopping at the first failure.

        Order is the order they were recorded, so a replayed backlog reads the
        same as a live one - `recorded_at` travels in the payload, so a late
        message is still stamped with when it happened.
        """
        spool = self._get_spool()
        if spool is None:
            return False
        delivered = 0
        with self._flush_lock:
            while not self._stop_event.is_set():
                batch = spool.pending(self.config.spool_batch)
                if not batch:
                    break
                sent_ids = []
                for queued in batch:
                    if not self._send(queued.topic, queued.payload):
                        break
                    sent_ids.append(queued.id)
                spool.discard(sent_ids)
                delivered += len(sent_ids)
                if len(sent_ids) < len(batch):
                    break
        return delivered > 0

    @property
    def spool_depth(self) -> int:
        """Messages waiting for the uplink. 0 when spooling is switched off."""
        spool = self._get_spool()
        return 0 if spool is None else spool.depth()

    def _send(self, topic: str, payload: str) -> bool:
        """One publish attempt against the uplink. Never raises."""
        try:
            connection = self._ensure_connection()
            connection.publish(topic=topic, payload=payload, qos=QOS_AT_LEAST_ONCE)
        except Exception as error:  # the uplink is allowed to fail
            self.failed += 1
            logger.warning("Telemetry publish failed", exc_info=error)
            self._disconnect()
            return False
        self.published += 1
        return True

    def _enqueue(self, payload: str) -> bool:
        """Write one message to the spool. False if there is no usable spool."""
        spool = self._get_spool()
        if spool is None:
            return False
        try:
            spool.append(self.config.resolved_topic, payload)
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

    def publish_if_due(self) -> bool:
        """Publish only when something changed, or the heartbeat came due.

        A parked rover produces thousands of identical snapshots a day; storing
        them costs disk and tells nobody anything. The heartbeat is what keeps
        silence meaningful: no message for more than one interval means the
        vehicle is gone, not idle.
        """
        if not self.configured:
            return False

        snapshot = self.snapshot()
        signature = json.dumps(significant(snapshot), sort_keys=True, default=_json_default)

        if signature != self._last_signature:
            return self.publish_once(snapshot, trigger="change")

        if self._due_for_heartbeat(snapshot):
            trigger = "idle" if self._is_idle(snapshot) else "heartbeat"
            return self.publish_once(snapshot, trigger=trigger)

        self.skipped += 1
        return False

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

    def _ensure_connection(self) -> Connection:
        with self._lock:
            if self._connection is None:
                connection = self._connection_factory(self.config)
                result = connection.connect()
                if hasattr(result, "result"):
                    result.result()
                self._connection = connection
            return self._connection

    def _disconnect(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
        if connection is None:
            return
        try:
            result = connection.disconnect()
            if hasattr(result, "result"):
                result.result()
        except Exception as error:
            logger.debug("Telemetry disconnect failed", exc_info=error)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            # A skipped tick still owes the backlog an attempt: without this a
            # parked rover would sit on spooled messages until it moved again.
            if not self.publish_if_due():
                self.flush()
            self._sleep(self.config.interval_seconds)
