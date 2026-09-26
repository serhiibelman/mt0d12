"""Tests for the MQTT publisher in `lib/telemetry`.

A fake connection stands in for AWS IoT, so nothing here needs the network,
a certificate or the `awscrt` extension. Spooling is left on, as it is in
production, but pointed at `:memory:` so no test writes the vehicle's spool.
"""

import json
from datetime import datetime, timezone

import pytest

from lib.telemetry import TelemetryConfig, TelemetryPublisher


class FakeFuture:
    def result(self, timeout=None):
        return None


class FakeConnection:
    def __init__(self, *, publish_error: Exception | None = None) -> None:
        self.published: list[tuple[str, str]] = []
        self.connects = 0
        self.disconnects = 0
        self.publish_error = publish_error

    def connect(self):
        self.connects += 1
        return FakeFuture()

    def publish(self, topic, payload, qos):
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((topic, payload))
        return FakeFuture()

    def go_offline(self, error: Exception | None = None):
        self.publish_error = error or OSError("uplink down")

    def go_online(self):
        self.publish_error = None

    def disconnect(self):
        self.disconnects += 1
        return FakeFuture()


STATE = {
    "overall_status": "ok",
    "motor_feedback": [{"motor_id": 1, "rpm": None}],
    "battery": {"voltage_v": 12.4, "current_a": 0.0, "remaining_percent": 76},
    "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 271.3},
}


def snapshot() -> dict:
    return {
        **STATE,
        "timestamp": datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
    }


def moving():
    """Put the shared snapshot into a state where a motor was commanded."""
    STATE["motor_feedback"] = [{"motor_id": 1, "rpm": 40}]


def make_publisher(connection=None, **overrides):
    fields = {
        "endpoint": "example-ats.iot.eu-central-1.amazonaws.com",
        "client_id": "rover-01",
        "thing_name": "rover-01",
        "topic": "rover/{thing}/telemetry",
        "cert_path": "/certs/device.pem.crt",
        "key_path": "/certs/private.pem.key",
        "root_ca_path": "/certs/Amazon-root-CA-1.pem",
        "spool_path": ":memory:",
    }
    fields.update(overrides)
    config = TelemetryConfig(**fields)
    connection = connection or FakeConnection()
    publisher = TelemetryPublisher(
        snapshot=snapshot,
        config=config,
        connection_factory=lambda _config: connection,
    )
    return publisher, connection


def test_no_endpoint_leaves_telemetry_switched_off():
    publisher, connection = make_publisher(endpoint="")

    publisher.start()

    assert publisher.configured is False
    assert publisher.publish_once() is False
    assert connection.connects == 0
    publisher.stop()


def test_publishes_to_the_topic_the_iot_rule_subscribes_to():
    publisher, connection = make_publisher()

    assert publisher.publish_once() is True

    topic, _payload = connection.published[0]
    assert topic == "rover/rover-01/telemetry"


def test_payload_is_json_with_the_thing_name_in_the_body():
    # The Lambda reads thing_name from the body, so it never has to parse the
    # topic; the IoT rule can stay a plain `SELECT *`.
    publisher, connection = make_publisher()

    publisher.publish_once()

    _topic, payload = connection.published[0]
    message = json.loads(payload)
    assert message["thing_name"] == "rover-01"
    assert message["snapshot"]["overall_status"] == "ok"


def test_datetimes_survive_serialisation_as_iso_strings():
    publisher, connection = make_publisher()

    publisher.publish_once()

    message = json.loads(connection.published[0][1])
    assert message["snapshot"]["timestamp"] == "2026-09-12T10:00:00+00:00"
    assert datetime.fromisoformat(message["recorded_at"]).tzinfo is not None


def test_the_connection_is_reused_across_publishes():
    publisher, connection = make_publisher()

    publisher.publish_once()
    publisher.publish_once()

    assert connection.connects == 1
    assert len(connection.published) == 2


def test_a_failed_publish_is_swallowed_and_drops_the_connection():
    # The uplink is allowed to fail: the driving loop must never see it, and
    # the next tick reconnects rather than reusing a dead socket.
    publisher, connection = make_publisher(connection=FakeConnection(publish_error=OSError("down")))

    assert publisher.publish_once() is False
    assert publisher.failed == 1
    assert publisher.published == 0
    assert connection.disconnects == 1


def test_stop_disconnects_cleanly():
    publisher, connection = make_publisher()
    publisher.publish_once()

    publisher.stop()

    assert connection.disconnects == 1


class FakePahoClient:
    """Stands in for `paho.mqtt.client.Client`, recording how it was driven."""

    last: "FakePahoClient | None" = None

    def __init__(self, callback_api_version, client_id, protocol, clean_session):
        self.client_id = client_id
        self.clean_session = clean_session
        self.tls = None
        self.connected_to = None
        self.loops_started = 0
        self.loops_stopped = 0
        self.disconnects = 0
        self.publish_rc = 0
        FakePahoClient.last = self

    def tls_set(self, ca_certs, certfile, keyfile, tls_version):
        self.tls = {"ca": ca_certs, "cert": certfile, "key": keyfile}

    def connect(self, host, port, keepalive):
        self.connected_to = (host, port, keepalive)

    def loop_start(self):
        self.loops_started += 1

    def loop_stop(self):
        self.loops_stopped += 1

    def publish(self, topic, payload, qos):
        rc = self.publish_rc

        class Info:
            def __init__(self) -> None:
                self.rc = rc

            def wait_for_publish(self, timeout=None):
                return None

        return Info()

    def disconnect(self):
        self.disconnects += 1


@pytest.fixture()
def fake_paho(monkeypatch):
    import paho.mqtt.client as mqtt

    monkeypatch.setattr(mqtt, "Client", FakePahoClient)
    return mqtt


def test_paho_connects_with_mutual_tls_on_the_iot_port(fake_paho):
    from lib.telemetry.publisher import _PahoConnection

    publisher, _ = make_publisher()
    connection = _PahoConnection(publisher.config)

    connection.connect()

    client = FakePahoClient.last
    assert client.connected_to == ("example-ats.iot.eu-central-1.amazonaws.com", 8883, 30)
    assert client.tls == {
        "ca": "/certs/Amazon-root-CA-1.pem",
        "cert": "/certs/device.pem.crt",
        "key": "/certs/private.pem.key",
    }
    # Without the network thread the QoS 1 handshake never completes.
    assert client.loops_started == 1
    assert client.client_id == "rover-01"


def test_a_nonzero_publish_return_code_is_an_error(fake_paho):
    from lib.telemetry.publisher import _PahoConnection

    publisher, _ = make_publisher()
    connection = _PahoConnection(publisher.config)
    connection.connect()
    FakePahoClient.last.publish_rc = 4

    with pytest.raises(RuntimeError, match="rc=4"):
        connection.publish("rover/rover-01/telemetry", "{}", 1)


def test_disconnect_stops_the_network_thread(fake_paho):
    from lib.telemetry.publisher import _PahoConnection

    publisher, _ = make_publisher()
    connection = _PahoConnection(publisher.config)
    connection.connect()

    connection.disconnect()

    assert FakePahoClient.last.loops_stopped == 1
    assert FakePahoClient.last.disconnects == 1


@pytest.fixture(autouse=True)
def reset_state():
    STATE.clear()
    STATE["overall_status"] = "ok"
    STATE["motor_feedback"] = [{"motor_id": 1, "rpm": None}]
    STATE["battery"] = {"voltage_v": 12.4, "current_a": 0.0, "remaining_percent": 76}
    STATE["attitude"] = {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 271.3}
    STATE["pi"] = {
        "cpu_temp_c": 51.5,
        "load_1m": 0.42,
        "memory_available_mb": 210,
        "memory_available_percent": 48,
        "disk_free_mb": 5120,
        "disk_free_percent": 62,
        "undervoltage_now": False,
        "undervoltage_since_boot": False,
        "warnings": [],
    }
    yield


def make_ticking_publisher(**overrides):
    """A publisher with a clock we control, so heartbeats are not a sleep."""
    clock = {"t": 0.0}
    fields = {
        "endpoint": "example-ats.iot.eu-central-1.amazonaws.com",
        "client_id": "rover-01",
        "thing_name": "rover-01",
        "topic": "rover/{thing}/telemetry",
        "cert_path": "/certs/device.pem.crt",
        "key_path": "/certs/private.pem.key",
        "root_ca_path": "/certs/Amazon-root-CA-1.pem",
        "spool_path": ":memory:",
        "heartbeat_seconds": 30.0,
        "idle_heartbeat_seconds": 300.0,
    }
    fields.update(overrides)
    connection = FakeConnection()
    publisher = TelemetryPublisher(
        snapshot=snapshot,
        config=TelemetryConfig(**fields),
        connection_factory=lambda _config: connection,
        time_func=lambda: clock["t"],
    )
    return publisher, connection, clock


def test_the_first_sample_is_always_published():
    publisher, connection, _ = make_ticking_publisher()

    assert publisher.publish_if_due() is True
    assert json.loads(connection.published[0][1])["trigger"] == "change"


def test_an_unchanged_snapshot_is_not_republished():
    # A parked rover would otherwise write thousands of identical rows a day.
    publisher, connection, _ = make_ticking_publisher()
    publisher.publish_if_due()

    assert publisher.publish_if_due() is False
    assert publisher.skipped == 1
    assert len(connection.published) == 1


def test_moving_clocks_alone_do_not_count_as_a_change():
    publisher, connection, _ = make_ticking_publisher()
    publisher.publish_if_due()

    # `timestamp`/`checked_at` advance on every sample by definition.
    STATE["unused"] = None
    del STATE["unused"]

    assert publisher.publish_if_due() is False
    assert len(connection.published) == 1


def test_a_changed_component_publishes_immediately():
    publisher, connection, _ = make_ticking_publisher()
    publisher.publish_if_due()

    STATE["overall_status"] = "degraded"

    assert publisher.publish_if_due() is True
    message = json.loads(connection.published[1][1])
    assert message["trigger"] == "change"
    assert message["snapshot"]["overall_status"] == "degraded"


def test_the_heartbeat_publishes_an_unchanged_snapshot():
    # Silence has to mean "gone", not "idle".
    publisher, connection, clock = make_ticking_publisher()
    moving()
    publisher.publish_if_due()

    clock["t"] = 29.0
    assert publisher.publish_if_due() is False

    clock["t"] = 30.0
    assert publisher.publish_if_due() is True
    assert json.loads(connection.published[1][1])["trigger"] == "heartbeat"


def test_a_zero_heartbeat_publishes_only_on_change():
    publisher, connection, clock = make_ticking_publisher(
        heartbeat_seconds=0, idle_heartbeat_seconds=0
    )
    publisher.publish_if_due()

    clock["t"] = 10_000.0

    assert publisher.publish_if_due() is False
    assert len(connection.published) == 1


def test_a_failed_publish_is_kept_rather_than_re_queued_every_tick():
    # The spool - not the change detector - is what stops the sample being
    # lost, so a change that failed to send counts as recorded. Otherwise an
    # outage would queue the same unchanged snapshot on every single tick and
    # push the interesting messages out through the retention cap.
    publisher, connection, _ = make_ticking_publisher()
    connection.go_offline()

    assert publisher.publish_if_due() is False
    assert publisher.publish_if_due() is False

    assert publisher.spool_depth == 1
    assert publisher.skipped == 1


def test_a_parked_rover_waits_for_the_idle_heartbeat():
    # The whole point: a resting vehicle has nothing to say, so it says it
    # rarely - 300s instead of 30s.
    publisher, connection, clock = make_ticking_publisher()
    publisher.publish_if_due()

    clock["t"] = 299.0
    assert publisher.publish_if_due() is False

    clock["t"] = 300.0
    assert publisher.publish_if_due() is True
    assert json.loads(connection.published[1][1])["trigger"] == "idle"


def test_a_moving_rover_keeps_the_normal_heartbeat():
    publisher, connection, clock = make_ticking_publisher()
    moving()
    publisher.publish_if_due()

    clock["t"] = 30.0

    assert publisher.publish_if_due() is True
    assert json.loads(connection.published[1][1])["trigger"] == "heartbeat"


def test_a_change_while_parked_still_publishes_immediately():
    # Quiet must not mean deaf: a component failing while parked is exactly
    # what you want to hear about.
    publisher, connection, clock = make_ticking_publisher()
    publisher.publish_if_due()

    clock["t"] = 10.0
    STATE["overall_status"] = "degraded"

    assert publisher.publish_if_due() is True
    assert json.loads(connection.published[1][1])["trigger"] == "change"


def test_moving_off_does_not_wait_for_the_idle_heartbeat():
    publisher, connection, clock = make_ticking_publisher()
    publisher.publish_if_due()

    clock["t"] = 5.0
    moving()

    assert publisher.publish_if_due() is True


def test_a_zero_idle_heartbeat_keeps_one_rate_for_both():
    publisher, connection, clock = make_ticking_publisher(idle_heartbeat_seconds=0)
    publisher.publish_if_due()

    clock["t"] = 30.0

    assert publisher.publish_if_due() is True
    assert json.loads(connection.published[1][1])["trigger"] == "heartbeat"


# -- the offline spool ------------------------------------------------------


class BrokenSpool:
    """A spool on a read-only card, or a corrupt file."""

    def append(self, topic, payload):
        raise OSError("attempt to write a readonly database")

    def pending(self, limit):
        return []

    def discard(self, ids):
        return None

    def depth(self):
        return 0

    def close(self):
        return None


def test_a_message_the_uplink_refused_goes_out_on_reconnect():
    # The whole point of the spool: losing the link is exactly when the
    # interesting samples happen, and they used to be logged and dropped.
    publisher, connection = make_publisher()
    connection.go_offline()
    assert publisher.publish_once() is False
    assert connection.published == []

    connection.go_online()

    assert publisher.flush() is True
    assert len(connection.published) == 1
    assert json.loads(connection.published[0][1])["snapshot"]["overall_status"] == "ok"


def test_a_backlog_replays_oldest_first():
    publisher, connection = make_publisher()
    connection.go_offline()
    for status in ("first", "second", "third"):
        STATE["overall_status"] = status
        publisher.publish_once()

    connection.go_online()
    publisher.flush()

    replayed = [
        json.loads(payload)["snapshot"]["overall_status"] for _t, payload in connection.published
    ]
    assert replayed == ["first", "second", "third"]


def test_a_replayed_message_keeps_the_time_it_was_recorded():
    # A late message still has to say when it happened, not when it was sent,
    # or the whole backlog lands in Postgres stamped with the reconnect.
    publisher, connection = make_publisher()
    connection.go_offline()
    publisher.publish_once()
    queued_by = datetime.now(timezone.utc)

    connection.go_online()
    publisher.flush()

    recorded_at = datetime.fromisoformat(json.loads(connection.published[0][1])["recorded_at"])
    assert recorded_at <= queued_by


def test_delivered_messages_leave_the_spool():
    publisher, connection = make_publisher()

    publisher.publish_once()

    assert publisher.spool_depth == 0
    assert len(connection.published) == 1


def test_a_backlog_larger_than_one_batch_is_fully_drained():
    publisher, connection = make_publisher(spool_batch=2)
    connection.go_offline()
    for index in range(5):
        STATE["overall_status"] = f"status-{index}"
        publisher.publish_once()

    connection.go_online()
    publisher.flush()

    assert len(connection.published) == 5
    assert publisher.spool_depth == 0


def test_a_flush_that_fails_part_way_keeps_the_rest():
    publisher, connection = make_publisher()
    connection.go_offline()
    for status in ("first", "second"):
        STATE["overall_status"] = status
        publisher.publish_once()
    connection.go_online()

    # One goes out, then the link drops again mid-drain.
    original_publish = connection.publish

    def flaky(topic, payload, qos):
        result = original_publish(topic, payload, qos)
        connection.go_offline()
        return result

    connection.publish = flaky
    publisher.flush()

    assert len(connection.published) == 1
    assert publisher.spool_depth == 1


def test_a_backlog_is_sent_even_when_nothing_new_is_due():
    # A parked rover skips its tick; without a flush there it would sit on the
    # backlog until something changed. This is the contract `_loop` relies on.
    publisher, connection = make_publisher()
    connection.go_offline()
    publisher.publish_if_due()
    connection.go_online()

    assert publisher.publish_if_due() is False
    assert publisher.flush() is True
    assert publisher.spool_depth == 0


def test_an_empty_spool_path_restores_publish_or_drop():
    # The off switch, and the behaviour every rover had before the spool.
    publisher, connection = make_publisher(spool_path="")
    connection.go_offline()

    assert publisher.publish_once() is False
    assert publisher.spool_depth == 0

    connection.go_online()
    assert publisher.flush() is False
    assert connection.published == []


def test_an_unwritable_spool_falls_back_to_publishing_direct():
    # A read-only card must cost the backlog, not the telemetry.
    publisher, connection = make_publisher()
    publisher._spool_factory = lambda _config: BrokenSpool()

    assert publisher.publish_once() is True
    assert len(connection.published) == 1


def test_the_spool_file_is_not_opened_until_something_is_published(tmp_path):
    # `create_app()` builds a publisher at import time, including on a laptop
    # with no telemetry configured; that must not leave a file behind.
    path = tmp_path / "spool.sqlite3"
    publisher, _connection = make_publisher(spool_path=str(path))

    assert not path.exists()

    publisher.publish_once()
    assert path.exists()
    publisher.stop()


# -- battery and attitude against the change detector -----------------------


def test_compass_drift_alone_does_not_wake_a_parked_rover():
    # A compass wanders on its own. Counting that as news would publish every
    # few seconds and undo the idle heartbeat entirely.
    publisher, connection, clock = make_ticking_publisher()
    publisher.publish_if_due()

    clock["t"] = 10.0
    STATE["attitude"] = {**STATE["attitude"], "yaw_deg": 274.8}

    assert publisher.publish_if_due() is False
    assert len(connection.published) == 1


def test_a_rover_that_tips_over_says_so_immediately():
    # Roll and pitch are gravity-referenced, so unlike yaw they stay put -
    # which is what makes them safe to treat as news.
    publisher, connection, clock = make_ticking_publisher()
    publisher.publish_if_due()

    clock["t"] = 10.0
    STATE["attitude"] = {**STATE["attitude"], "roll_deg": 47.2}

    assert publisher.publish_if_due() is True
    assert json.loads(connection.published[1][1])["trigger"] == "change"


def test_a_falling_battery_is_news():
    publisher, connection, clock = make_ticking_publisher()
    publisher.publish_if_due()

    clock["t"] = 10.0
    STATE["battery"] = {**STATE["battery"], "voltage_v": 11.9}

    assert publisher.publish_if_due() is True


def test_yaw_still_travels_in_the_payload():
    # Excluded from change detection, not from the message: the heading is
    # still the thing you want when reading back what the rover was doing.
    publisher, connection, _ = make_ticking_publisher()

    publisher.publish_if_due()

    assert json.loads(connection.published[0][1])["snapshot"]["attitude"]["yaw_deg"] == 271.3


# -- Pi health against the change detector ----------------------------------


def test_drifting_pi_gauges_alone_do_not_wake_a_parked_rover():
    # Temperature, load and memory move on every reading; counting that as
    # news would publish every few seconds, like compass drift would.
    publisher, connection, clock = make_ticking_publisher()
    publisher.publish_if_due()

    clock["t"] = 10.0
    STATE["pi"] = {
        **STATE["pi"],
        "cpu_temp_c": 53.2,
        "load_1m": 0.91,
        "memory_available_mb": 198,
        "memory_available_percent": 45,
        "disk_free_mb": 5119,
    }

    assert publisher.publish_if_due() is False
    assert len(connection.published) == 1


def test_undervoltage_is_news_immediately():
    publisher, connection, clock = make_ticking_publisher()
    publisher.publish_if_due()

    clock["t"] = 10.0
    STATE["pi"] = {
        **STATE["pi"],
        "undervoltage_now": True,
        "undervoltage_since_boot": True,
        "warnings": ["undervoltage"],
    }

    assert publisher.publish_if_due() is True
    assert json.loads(connection.published[1][1])["trigger"] == "change"


def test_crossing_a_threshold_is_news_even_though_the_gauge_is_not():
    publisher, connection, clock = make_ticking_publisher()
    publisher.publish_if_due()

    clock["t"] = 10.0
    STATE["pi"] = {**STATE["pi"], "cpu_temp_c": 81.0, "warnings": ["cpu_hot"]}

    assert publisher.publish_if_due() is True
    assert json.loads(connection.published[1][1])["snapshot"]["pi"]["cpu_temp_c"] == 81.0
