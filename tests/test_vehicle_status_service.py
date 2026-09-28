from apps.api.services.vehicle_status import VehicleStatusService


class FakeMotor:
    def __init__(self, device: str):
        self.device = device
        self.commands: list[tuple[int, int]] = []
        self.closed = False

    def send_rpm(self, motor_id: int, rpm: int = 0) -> None:
        self.commands.append((motor_id, rpm))

    def close(self) -> None:
        self.closed = True


def test_start_motors_ramps_all_motors() -> None:
    motors: list[FakeMotor] = []

    def motor_factory(*, device: str) -> FakeMotor:
        motor = FakeMotor(device)
        motors.append(motor)
        return motor

    service = VehicleStatusService(
        motor_device="/dev/test",
        fc_device=None,
        motor_factory=motor_factory,
        sleep_func=lambda _: None,
    )

    response = service.start_motors(12)

    assert response["action"] == "start"
    assert response["current_rpm"] == 12
    assert len(motors) == 1
    assert motors[0].closed is True
    assert motors[0].commands == [
        (3, 5),
        (4, 5),
        (1, -5),
        (2, -5),
        (3, 10),
        (4, 10),
        (1, -10),
        (2, -10),
        (3, 12),
        (4, 12),
        (1, -12),
        (2, -12),
    ]


def test_stop_motors_ramps_down_from_current_speed() -> None:
    motors: list[FakeMotor] = []

    def motor_factory(*, device: str) -> FakeMotor:
        motor = FakeMotor(device)
        motors.append(motor)
        return motor

    service = VehicleStatusService(
        motor_device="/dev/test",
        fc_device=None,
        motor_factory=motor_factory,
        sleep_func=lambda _: None,
    )

    service.start_motors(12)
    response = service.stop_motors()

    assert response["action"] == "stop"
    assert response["current_rpm"] == 0
    assert len(motors) == 2
    assert motors[1].commands == [
        (3, 7),
        (4, 7),
        (1, -7),
        (2, -7),
        (3, 2),
        (4, 2),
        (1, -2),
        (2, -2),
        (3, 0),
        (4, 0),
        (1, 0),
        (2, 0),
    ]


# -- driving from the status page -------------------------------------------


def make_drive_service() -> tuple[VehicleStatusService, list[FakeMotor]]:
    motors: list[FakeMotor] = []

    def motor_factory(*, device: str) -> FakeMotor:
        motor = FakeMotor(device)
        motors.append(motor)
        return motor

    service = VehicleStatusService(
        motor_device="/dev/test",
        fc_device=None,
        motor_factory=motor_factory,
        sleep_func=lambda _: None,
        pi_health_reader=dict,
    )
    return service, motors


def test_a_driver_holds_one_port_open_for_the_whole_session() -> None:
    service, motors = make_drive_service()
    driver = object()

    service.open_drive(driver)
    service.drive(driver, 50, 30)
    service.drive(driver, 60, 40)

    assert len(motors) == 1
    assert motors[0].closed is False
    # Right side negated for its mounting, as everywhere else.
    assert motors[0].commands == [(3, 50), (4, 50), (1, -30), (2, -30)] + [
        (3, 60),
        (4, 60),
        (1, -40),
        (2, -40),
    ]
    assert [m["rpm"] for m in service.snapshot()["motor_feedback"]] == [60, 60, -40, -40]


def test_closing_the_drive_stops_the_motors_and_releases_the_port() -> None:
    service, motors = make_drive_service()
    driver = object()
    service.open_drive(driver)
    service.drive(driver, 50, 50)

    service.close_drive(driver)

    assert motors[0].commands[-4:] == [(3, 0), (4, 0), (1, 0), (2, 0)]
    assert motors[0].closed is True
    # Closed means closed: a command still in flight when it happened is dropped.
    service.drive(driver, 50, 50)
    assert motors[0].commands[-1] == (2, 0)


def test_only_one_driver_at_a_time() -> None:
    service, _ = make_drive_service()
    first, second = object(), object()
    service.open_drive(first)

    try:
        service.open_drive(second)
    except RuntimeError as exc:
        assert "Another viewer is driving" in str(exc)
    else:
        raise AssertionError("a second driver must be refused")


def test_a_session_that_ended_cannot_touch_the_next_ones_bus() -> None:
    service, motors = make_drive_service()
    old, new = object(), object()
    service.open_drive(old)
    service.close_drive(old)
    service.open_drive(new)

    service.drive(old, 100, 100)
    service.close_drive(old)

    assert motors[1].commands == []
    assert motors[1].closed is False


def test_a_ramp_is_refused_while_someone_drives() -> None:
    service, _ = make_drive_service()
    service.open_drive(object())

    try:
        service.start_motors(50)
    except RuntimeError as exc:
        assert "being driven" in str(exc)
    else:
        raise AssertionError("/motors/start must not share the bus with a driver")


def test_the_probe_does_not_reopen_a_port_a_driver_holds() -> None:
    service, motors = make_drive_service()
    service.open_drive(object())

    service._probe_once()

    assert len(motors) == 1
    assert service.snapshot()["components"]["motor_bus"]["connected"] is True


def test_stopping_the_service_stops_a_driver_that_is_still_going() -> None:
    service, motors = make_drive_service()
    driver = object()
    service.open_drive(driver)
    service.drive(driver, 80, 80)

    service.stop()

    assert motors[0].commands[-4:] == [(3, 0), (4, 0), (1, 0), (2, 0)]
    assert motors[0].closed is True


# -- the flight controller, through the service ---------------------------
# The stream itself is tested in test_flight_controller.py.


def test_an_unconfigured_flight_controller_reports_nothing_known() -> None:
    service = VehicleStatusService(motor_device=None, fc_device=None, sleep_func=lambda _: None)
    service._probe_once()

    snapshot = service.snapshot()

    assert snapshot["battery"] == {
        "voltage_v": None,
        "current_a": None,
        "remaining_percent": None,
    }
    assert snapshot["attitude"] == {"roll_deg": None, "pitch_deg": None, "yaw_deg": None}
    assert snapshot["components"]["flight_controller"]["configured"] is False
    assert snapshot["overall_status"] == "degraded"
