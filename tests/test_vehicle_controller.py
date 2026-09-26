from apps.vehicle_control.vehicle_controller import VehicleController
from lib.gamepad.state import AxesState, ButtonsState, ControllerState
from settings import LEFT_SIDE, RIGHT_SIDE

ALL_MOTORS = LEFT_SIDE + RIGHT_SIDE


class FakeReceiver:
    def __init__(self) -> None:
        self.queue: list[ControllerState] = []

    def receive(self):
        return self.queue.pop(0) if self.queue else None


class FakeMotor:
    def __init__(self) -> None:
        self.commands: list[tuple[int, int]] = []

    def send_rpm(self, motor_id: int, rpm: int = 0) -> None:
        self.commands.append((motor_id, rpm))

    def set_brake(self, motor_id: int) -> None:
        self.commands.append((motor_id, "brake"))


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def packet(*, a: bool = False, left_y: float = 0.0) -> ControllerState:
    return ControllerState(
        timestamp=0.0,
        axes=AxesState(
            left_x=0.0,
            left_y=left_y,
            right_x=0.0,
            right_y=0.0,
            trigger_left=False,
            trigger_right=False,
        ),
        buttons=ButtonsState(a=a, b=False, x=False, y=False, lb=False, rb=False, l=False, r=False),
    )


def build():
    receiver, motor, clock = FakeReceiver(), FakeMotor(), FakeClock()
    controller = VehicleController(receiver, motor, link_timeout=0.5, time_func=clock)
    return controller, receiver, motor, clock


def drive_forward(controller, receiver, clock) -> None:
    """Arm with 'a', then hold the stick forward long enough to build speed."""
    receiver.queue.append(packet(a=True))
    controller.tick()
    for _ in range(10):
        clock.now += 0.05
        receiver.queue.append(packet(left_y=-1.0))
        controller.tick()


def test_silence_past_timeout_stops_all_motors() -> None:
    controller, receiver, motor, clock = build()
    drive_forward(controller, receiver, clock)
    assert controller._current_rpm > 0

    motor.commands.clear()
    clock.now += 0.6
    controller.tick()

    assert motor.commands == [(motor_id, 0) for motor_id in ALL_MOTORS]
    assert controller._drive_enabled is False
    assert controller._current_rpm == 0.0


def test_short_gap_does_not_stop() -> None:
    controller, receiver, motor, clock = build()
    drive_forward(controller, receiver, clock)

    motor.commands.clear()
    clock.now += 0.4
    controller.tick()

    assert motor.commands == []
    assert controller._drive_enabled is True


def test_stop_is_sent_once_per_outage() -> None:
    controller, receiver, motor, clock = build()
    drive_forward(controller, receiver, clock)

    motor.commands.clear()
    for _ in range(5):
        clock.now += 0.6
        controller.tick()

    assert len(motor.commands) == len(ALL_MOTORS)


def test_no_failsafe_before_first_packet() -> None:
    controller, _, motor, clock = build()

    clock.now += 10
    controller.tick()

    assert motor.commands == []


def test_restored_link_stays_disarmed_until_a_is_pressed_again() -> None:
    controller, receiver, motor, clock = build()
    drive_forward(controller, receiver, clock)
    clock.now += 0.6
    controller.tick()

    # Link returns with the stick still pushed and 'a' still held down.
    motor.commands.clear()
    clock.now += 0.05
    receiver.queue.append(packet(a=True, left_y=-1.0))
    controller.tick()

    assert controller._drive_enabled is False
    assert all(rpm == 0 for _, rpm in motor.commands)

    # Release, then press: that is a deliberate re-arm.
    for a in (False, True):
        clock.now += 0.05
        receiver.queue.append(packet(a=a))
        controller.tick()

    assert controller._drive_enabled is True


def test_second_outage_is_detected_after_recovery() -> None:
    controller, receiver, motor, clock = build()
    drive_forward(controller, receiver, clock)
    clock.now += 0.6
    controller.tick()

    clock.now += 0.05
    receiver.queue.append(packet())
    controller.tick()

    motor.commands.clear()
    clock.now += 0.6
    controller.tick()

    assert motor.commands == [(motor_id, 0) for motor_id in ALL_MOTORS]
