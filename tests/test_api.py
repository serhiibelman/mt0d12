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
