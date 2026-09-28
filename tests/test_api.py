import time

import pytest
from fastapi.testclient import TestClient


def test_health_endpoint(build_app) -> None:
    with TestClient(build_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "mt0d12-vehicle-api"
    assert payload["status"] == "degraded"
    assert "motor_bus" in payload["components"]


def test_status_endpoint(build_app) -> None:
    with TestClient(build_app()) as client:
        response = client.get("/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["motor_ids"]["left"] == [3, 4]
    assert payload["motor_ids"]["right"] == [1, 2]
    assert payload["motor_feedback"][0]["motor_id"] == 1
    assert payload["motor_feedback"][0]["rpm"] is None


def test_status_endpoint_reports_battery_and_attitude(build_app) -> None:
    # /status is the snapshot an operator reads; battery is the field that
    # predicts the failure that actually strands the vehicle.
    with TestClient(build_app()) as client:
        payload = client.get("/status").json()

    assert payload["battery"] == {
        "voltage_v": 12.4,
        "current_a": 1.83,
        "remaining_percent": 76,
    }
    assert payload["attitude"]["roll_deg"] == 0.4


def test_status_endpoint_reports_pi_health(build_app) -> None:
    with TestClient(build_app()) as client:
        payload = client.get("/status").json()

    assert payload["pi"]["cpu_temp_c"] == 51.5
    assert payload["pi"]["undervoltage_since_boot"] is True
    assert payload["pi"]["warnings"] == []


def test_start_motors_endpoint(build_app, vehicle_service) -> None:
    with TestClient(build_app()) as client:
        response = client.post("/motors/start", json={"rpm": 120})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "start"
    assert payload["target_rpm"] == 120
    assert vehicle_service.started_rpms == [120]


def test_stop_motors_endpoint(build_app, vehicle_service) -> None:
    with TestClient(build_app()) as client:
        response = client.post("/motors/stop")

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "stop"
    assert payload["current_rpm"] == 0
    assert vehicle_service.stop_calls == 1


def test_camera_stream_endpoint(build_app, camera_service) -> None:
    with TestClient(build_app()) as client:
        response = client.get("/camera/stream")

    assert response.status_code == 200
    assert camera_service.slots == 0, "the viewer slot must be released when the stream ends"
    assert response.headers["content-type"] == "multipart/x-mixed-replace; boundary=FRAME"
    body = response.content
    assert body.count(b"--FRAME") == 2
    assert b"Content-Type: image/jpeg" in body
    assert b"Content-Length: 5" in body
    assert body.endswith(b"second\r\n")


def test_camera_stream_returns_503_when_camera_is_unavailable(
    build_app, make_camera_service
) -> None:
    app = build_app(camera=make_camera_service(available=False))

    with TestClient(app) as client:
        response = client.get("/camera/stream")

    assert response.status_code == 503
    assert "no camera detected" in response.json()["detail"]


def test_camera_snapshot_endpoint(build_app) -> None:
    with TestClient(build_app()) as client:
        response = client.get("/camera/snapshot")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"jpeg-bytes"


def test_camera_status_endpoint(build_app) -> None:
    with TestClient(build_app()) as client:
        response = client.get("/camera/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is True
    assert payload["frames_captured"] == 12
    assert payload["component"]["connected"] is True


def test_camera_start_and_stop_endpoints(build_app, camera_service) -> None:
    with TestClient(build_app()) as client:
        start_response = client.post("/camera/start")
        stop_response = client.post("/camera/stop")

    assert start_response.json()["running"] is True
    assert stop_response.json()["running"] is False
    assert camera_service.start_calls == 1
    # The app also stops the camera on shutdown.
    assert camera_service.stop_calls == 2


def test_root_serves_the_status_page(build_app) -> None:
    with TestClient(build_app()) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # The page is only useful if it talks to the stream this API serves.
    assert "/ws/status" in response.text


# -- /ws/status --------------------------------------------------------------


def test_status_stream_sends_the_same_snapshot_as_status(build_app) -> None:
    with TestClient(build_app()) as client:
        polled = client.get("/status").json()
        with client.websocket_connect("/ws/status") as ws:
            pushed = ws.receive_json()

    # Same schema, same data. The clocks (`timestamp`, `checked_at`) move
    # between two reads, so the comparison is on everything that does not.
    assert pushed.keys() == polled.keys()
    for section in ("battery", "attitude", "pi", "motor_ids", "motor_feedback"):
        assert pushed[section] == polled[section]


def test_status_stream_keeps_pushing(build_app, vehicle_service) -> None:
    with TestClient(build_app()) as client, client.websocket_connect("/ws/status") as ws:
        first = ws.receive_json()
        vehicle_service.voltage = 11.9
        second = ws.receive_json()

    assert first["battery"]["voltage_v"] == 12.4
    assert second["battery"]["voltage_v"] == 11.9


def test_a_message_from_the_viewer_does_not_break_the_stream(build_app) -> None:
    with TestClient(build_app()) as client, client.websocket_connect("/ws/status") as ws:
        ws.receive_json()
        ws.send_text("hello")
        assert ws.receive_json()["service"] == "mt0d12-vehicle-api"


def test_two_viewers_watch_at_once(build_app, vehicle_service) -> None:
    with (
        TestClient(build_app()) as client,
        client.websocket_connect("/ws/status") as first,
        client.websocket_connect("/ws/status") as second,
    ):
        first.receive_json()
        second.receive_json()
        vehicle_service.voltage = 11.9
        # Both are fed by the same producer, so both see the change.
        for ws in (first, second):
            while ws.receive_json()["battery"]["voltage_v"] != 11.9:
                pass


# -- driving over /ws/status -------------------------------------------------


def next_drive_state(ws) -> dict:
    """The next drive-state message, skipping the status updates between."""
    while True:
        message = ws.receive_json()
        if message.get("type") == "drive":
            return message


def wait_for(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_arming_and_driving_moves_the_motors(build_app, vehicle_service) -> None:
    with TestClient(build_app()) as client, client.websocket_connect("/ws/status") as ws:
        ws.send_json({"type": "arm"})
        assert next_drive_state(ws) == {"type": "drive", "armed": True, "detail": "Driving"}
        ws.send_json({"type": "drive", "throttle": 1.0, "steer": 0.0})
        wait_for(lambda: vehicle_service.drive_commands)
        # Status keeps coming while driving.
        assert "battery" in ws.receive_json()

    assert vehicle_service.drive_commands[0] == (5, 5)


def test_drive_commands_without_arming_are_ignored(build_app, vehicle_service) -> None:
    with TestClient(build_app()) as client, client.websocket_connect("/ws/status") as ws:
        ws.send_json({"type": "drive", "throttle": 1.0, "steer": 0.0})
        ws.receive_json()
        ws.receive_json()

    assert vehicle_service.drive_commands == []
    assert vehicle_service.drive_owner is None


def test_malformed_commands_do_not_break_the_stream(build_app, vehicle_service) -> None:
    with TestClient(build_app()) as client, client.websocket_connect("/ws/status") as ws:
        ws.send_json({"type": "arm"})
        next_drive_state(ws)
        for junk in ("[1, 2]", '{"type": "drive", "throttle": "fast", "steer": 0}', "{"):
            ws.send_text(junk)
        ws.send_bytes(b"\x00")
        ws.send_json({"type": "drive", "throttle": 0.0, "steer": 0.5})
        wait_for(lambda: vehicle_service.drive_commands)

    assert vehicle_service.drive_commands == [(50, -50)]


def test_stop_disarms_and_releases_the_bus(build_app, vehicle_service) -> None:
    with TestClient(build_app()) as client, client.websocket_connect("/ws/status") as ws:
        ws.send_json({"type": "arm"})
        next_drive_state(ws)
        ws.send_json({"type": "stop"})
        assert next_drive_state(ws) == {"type": "drive", "armed": False, "detail": "Stopped"}
        assert vehicle_service.drive_owner is None


def test_leaving_while_driving_stops_the_motors(build_app, vehicle_service) -> None:
    with TestClient(build_app()) as client:
        with client.websocket_connect("/ws/status") as ws:
            ws.send_json({"type": "arm"})
            next_drive_state(ws)
            ws.send_json({"type": "drive", "throttle": 1.0, "steer": 0.0})
            wait_for(lambda: vehicle_service.drive_commands)
        wait_for(lambda: vehicle_service.drive_owner is None)

    assert vehicle_service.drive_closes == 1


def test_a_silent_driver_is_stopped(build_app, vehicle_service) -> None:
    app = build_app(drive_link_timeout=0.05)
    with TestClient(app) as client, client.websocket_connect("/ws/status") as ws:
        ws.send_json({"type": "arm"})
        next_drive_state(ws)
        # ... and then nothing, as from a laptop that went to sleep.
        state = next_drive_state(ws)
        assert state["armed"] is False
        assert "No command for 0.05s" in state["detail"]
        assert vehicle_service.drive_owner is None
        # Commands after the link comes back move nothing until armed again.
        ws.send_json({"type": "drive", "throttle": 1.0, "steer": 0.0})
        ws.receive_json()
        ws.receive_json()
        assert vehicle_service.drive_commands == []


def test_a_watcher_is_never_timed_out(build_app, vehicle_service) -> None:
    app = build_app(drive_link_timeout=0.01)
    with TestClient(app) as client, client.websocket_connect("/ws/status") as ws:
        for _ in range(10):
            assert "type" not in ws.receive_json()


def test_a_second_driver_is_refused(build_app, vehicle_service) -> None:
    with (
        TestClient(build_app()) as client,
        client.websocket_connect("/ws/status") as first,
        client.websocket_connect("/ws/status") as second,
    ):
        first.send_json({"type": "arm"})
        assert next_drive_state(first)["armed"] is True
        second.send_json({"type": "arm"})
        assert next_drive_state(second) == {
            "type": "drive",
            "armed": False,
            "detail": "Another viewer is driving",
        }
        # The second viewer leaving must not stop the first one's motors.
        second.close()
        first.send_json({"type": "drive", "throttle": 1.0, "steer": 0.0})
        wait_for(lambda: vehicle_service.drive_commands)
        assert vehicle_service.drive_closes == 0
