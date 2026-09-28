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


# -- battery and attitude from the flight controller ------------------------


class FakeMessage:
    def __init__(self, kind: str, **fields):
        self._kind = kind
        for name, value in fields.items():
            setattr(self, name, value)

    def get_type(self) -> str:
        return self._kind

    def get_srcSystem(self) -> int:
        return 1


def sys_status(voltage=12400, current=183, remaining=76) -> FakeMessage:
    """SYS_STATUS as MAVLink sends it: millivolts, centiamps, percent."""
    return FakeMessage(
        "SYS_STATUS",
        voltage_battery=voltage,
        current_battery=current,
        battery_remaining=remaining,
    )


def attitude(roll=0.0, pitch=0.0, yaw=0.0) -> FakeMessage:
    """ATTITUDE as MAVLink sends it: radians."""
    return FakeMessage("ATTITUDE", roll=roll, pitch=pitch, yaw=yaw)


class FakeLink:
    """Stands in for a pymavlink connection, handing out queued messages."""

    def __init__(self, messages=None, *, heartbeat=True):
        self.messages = list(messages or [])
        self.heartbeat = FakeMessage("HEARTBEAT") if heartbeat else None
        self.closed = False

    def wait_heartbeat(self, timeout=None):
        return self.heartbeat

    def recv_match(self, type=None, blocking=False, timeout=None):
        while self.messages:
            message = self.messages.pop(0)
            if type is None or message.get_type() in type:
                return message
        return None

    def close(self) -> None:
        self.closed = True


def make_fc_service(link, **overrides) -> VehicleStatusService:
    fields = {
        "motor_device": None,
        "fc_device": "/dev/serial0",
        "fc_factory": lambda *_args, **_kwargs: link,
        "sleep_func": lambda _: None,
    }
    fields.update(overrides)
    return VehicleStatusService(**fields)


def probe_once(service: VehicleStatusService) -> dict:
    """Run one probe pass without starting the background thread."""
    service._probe_once()
    return service.snapshot()


def test_battery_is_converted_out_of_the_units_mavlink_uses() -> None:
    # SYS_STATUS reports mV, cA and %; nothing downstream should have to know.
    service = make_fc_service(FakeLink([sys_status(voltage=12400, current=183, remaining=76)]))

    battery = probe_once(service)["battery"]

    assert battery == {"voltage_v": 12.4, "current_a": 1.83, "remaining_percent": 76}


def test_attitude_is_converted_from_radians_to_degrees() -> None:
    service = make_fc_service(FakeLink([attitude(roll=0.0175, pitch=-0.0349, yaw=3.1416)]))

    assert probe_once(service)["attitude"] == {
        "roll_deg": 1.0,
        "pitch_deg": -2.0,
        "yaw_deg": 180.0,
    }


def test_the_unknown_sentinels_become_null_rather_than_a_65_volt_battery() -> None:
    # MAVLink reports "no reading" in band, with values that look plausible.
    service = make_fc_service(FakeLink([sys_status(voltage=65535, current=-1, remaining=-1)]))

    assert probe_once(service)["battery"] == {
        "voltage_v": None,
        "current_a": None,
        "remaining_percent": None,
    }


def test_both_readings_arrive_from_one_probe() -> None:
    service = make_fc_service(FakeLink([attitude(roll=0.0175), sys_status()]))

    snapshot = probe_once(service)

    assert snapshot["battery"]["voltage_v"] == 12.4
    assert snapshot["attitude"]["roll_deg"] == 1.0


def test_an_unconfigured_flight_controller_reports_nothing_known() -> None:
    service = VehicleStatusService(motor_device=None, fc_device=None, sleep_func=lambda _: None)

    snapshot = probe_once(service)

    assert snapshot["battery"] == {
        "voltage_v": None,
        "current_a": None,
        "remaining_percent": None,
    }
    assert snapshot["attitude"] == {"roll_deg": None, "pitch_deg": None, "yaw_deg": None}


def test_a_port_that_opens_but_never_speaks_is_not_connected() -> None:
    # wait_heartbeat returns None on timeout rather than raising, so a silent
    # FC used to report as connected with a heartbeat "from system None".
    service = make_fc_service(FakeLink(heartbeat=False))

    snapshot = probe_once(service)

    assert snapshot["components"]["flight_controller"]["connected"] is False
    assert "No heartbeat" in snapshot["components"]["flight_controller"]["detail"]
    assert snapshot["overall_status"] == "degraded"


def test_a_probe_that_catches_no_sys_status_keeps_the_last_battery() -> None:
    # SYS_STATUS streams slower than the probe runs. Blanking the battery on
    # every quiet window would make it flicker for no reason.
    link = FakeLink([sys_status(voltage=12400)])
    service = make_fc_service(link)
    probe_once(service)

    link.messages = [attitude(roll=0.0175)]
    service._probe_once()

    assert service.snapshot()["battery"]["voltage_v"] == 12.4


def test_readings_are_dropped_when_the_link_goes_away() -> None:
    # A value from a link that has since died would read as current.
    link = FakeLink([sys_status(voltage=12400)])
    service = make_fc_service(link)
    probe_once(service)

    link.heartbeat = None
    service._probe_once()

    assert service.snapshot()["battery"]["voltage_v"] is None
    assert service.snapshot()["attitude"]["roll_deg"] is None


def test_a_failing_read_costs_the_reading_not_the_health_check() -> None:
    class Exploding(FakeLink):
        def recv_match(self, **_kwargs):
            raise ValueError("malformed frame")

    service = make_fc_service(Exploding())

    snapshot = probe_once(service)

    assert snapshot["components"]["flight_controller"]["connected"] is True
    assert snapshot["battery"]["voltage_v"] is None


def test_the_link_is_closed_even_when_the_read_fails() -> None:
    class Exploding(FakeLink):
        def recv_match(self, **_kwargs):
            raise ValueError("malformed frame")

    link = Exploding()
    probe_once(make_fc_service(link))

    assert link.closed is True
