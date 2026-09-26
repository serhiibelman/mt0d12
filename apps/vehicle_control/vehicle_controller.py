import time
from typing import Callable, Optional

from lib.common.formatting import print_info, print_warning
from lib.gamepad.udp_receiver import UDPReceiver
from lib.gamepad.state import ControllerState
from lib.ddsm115 import DDS115
from settings import RIGHT_SIDE, LEFT_SIDE

MAX_RPM = 200
MAX_STEER_RPM = 100  # half of MAX_RPM for gentler turns
RAMP_STEP = 5
DEAD_ZONE = 0.1  # ignore axis jitter near center
LOOP_INTERVAL = 0.05  # 20 Hz
# The controller sends at 20 Hz whether or not the sticks move, so this much
# silence is ~10 lost packets in a row: the link is gone, not just lossy.
LINK_TIMEOUT = 0.5


class VehicleController:
    """Controls vehicle motors based on gamepad input received over UDP.

    Control scheme:
        - Press 'a' to toggle drive mode on/off.
        - Left joystick Y  → forward (up) / backward (down), RPM ramps smoothly.
        - Right joystick X → steering; blends with base RPM so the same input
          produces differential steering while moving and a tank turn when stopped.
        - 'lb' button      → brake all motors and disable drive mode.
        - No packet for LINK_TIMEOUT → stop all motors and disable drive mode;
          'a' must be pressed again once the link is back.

    Motor wiring convention (from actuator_test.py):
        RIGHT_SIDE motors receive rpm * -1 to match the physical mounting direction.
    """

    def __init__(
        self,
        receiver: UDPReceiver,
        motor: DDS115,
        link_timeout: float = LINK_TIMEOUT,
        time_func: Callable[[], float] = time.monotonic,
    ):
        self.receiver = receiver
        self.motor = motor
        self.link_timeout = link_timeout
        self._now = time_func
        self._current_rpm: float = 0.0
        self._braked: bool = False
        self._drive_enabled: bool = False
        self._prev_a: bool = False  # for edge detection on 'a' toggle
        # Arrival time on this clock, not the packet's `timestamp`: that one is
        # stamped by the laptop, whose clock need not agree with the Pi's.
        self._last_packet_at: Optional[float] = None
        self._link_lost: bool = False

    def run(self):
        """Start the main control loop. Blocks until KeyboardInterrupt."""
        print_info("VehicleController started")
        try:
            while True:
                self.tick()
                time.sleep(LOOP_INTERVAL)
        except KeyboardInterrupt:
            print_info("VehicleController stopped")
        finally:
            self._stop_motors()

    def tick(self) -> None:
        """One loop iteration: act on the newest packet, or fail safe on silence."""
        state = self._receive_latest()
        if state is not None:
            self._last_packet_at = self._now()
            if self._link_lost:
                self._link_lost = False
                print_info("Control link restored - press 'a' to drive")
            self._handle(state)
        elif self._link_timed_out():
            self._fail_safe()

    def _receive_latest(self) -> Optional[ControllerState]:
        """
        Drain the UDP buffer and return only the most recent state.

        send_rpm() blocks per motor, so packets pile up between iterations.
        Without draining, we always act on stale data.
        """
        latest = None
        while True:
            pkt = self.receiver.receive()
            if pkt is None:
                break
            latest = pkt
        return latest

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle(self, state: ControllerState) -> None:
        """Process one controller state snapshot and update motor outputs."""
        a_now = state.buttons.a
        if a_now and not self._prev_a:
            self._drive_enabled = not self._drive_enabled
            print_info(f"Drive {'ENABLED' if self._drive_enabled else 'DISABLED'}")
        self._prev_a = a_now

        print(
            f"drive={self._drive_enabled} lb={state.buttons.lb} "
            f"left_y={state.axes.left_y:+.2f} right_x={state.axes.right_x:+.2f} "
            f"rpm={self._current_rpm:.0f}"
        )

        if state.buttons.lb:
            self._brake()
            self._drive_enabled = False
            return

        self._braked = False

        if self._drive_enabled:
            left_y = state.axes.left_y  # left joystick up/down: up = -1, down = +1
            right_x = state.axes.right_x  # right joystick left/right: left = -1, right = +1

            # Apply dead zone to forward/back so releasing the stick ramps to 0 smoothly
            target_rpm = 0.0 if abs(left_y) < DEAD_ZONE else -left_y * MAX_RPM
            self._current_rpm = self._ramp_toward(self._current_rpm, target_rpm)

            left_rpm, right_rpm = self._compute_side_rpms(self._current_rpm, right_x)
        else:
            # Drive off: ramp back to zero gradually
            self._current_rpm = self._ramp_toward(self._current_rpm, 0.0)
            left_rpm = self._current_rpm
            right_rpm = self._current_rpm

        self._apply(left_rpm, right_rpm)

    def _link_timed_out(self) -> bool:
        """True once packets stop for longer than link_timeout.

        Never true before the first packet: until a controller has spoken the
        motors have not been commanded, so there is nothing to stop.
        """
        if self._link_lost or self._last_packet_at is None:
            return False
        return self._now() - self._last_packet_at > self.link_timeout

    def _fail_safe(self) -> None:
        """Stop the motors and disarm once the control link has gone quiet.

        The motors hold their last commanded RPM, so without this a laptop that
        sleeps or a Wi-Fi drop mid-drive leaves the rover going at full speed.
        Stops at once rather than ramping: a ramp from full speed takes seconds
        of driving blind. Drive stays off until 'a' is pressed again, so a link
        that comes back with the stick still pushed does not lurch forward.
        """
        print_warning(f"Control link lost for >{self.link_timeout:.1f}s - stopping motors")
        self._link_lost = True
        self._drive_enabled = False
        # A held 'a' must be released and pressed again, not read as a fresh press.
        self._prev_a = True
        self._current_rpm = 0.0
        self._stop_motors()

    def _compute_side_rpms(self, base_rpm: float, right_x: float) -> tuple[float, float]:
        """
        Return (left_rpm, right_rpm) by blending base speed with steering input.

        Adds the steering component to the left side and subtracts it from the
        right side, then clamps both to ±MAX_RPM.  When base_rpm is zero this
        produces a tank turn; when the vehicle is moving it gives differential
        steering.
        """
        steer = 0.0 if abs(right_x) < DEAD_ZONE else right_x * MAX_STEER_RPM
        left_rpm = max(-MAX_RPM, min(MAX_RPM, base_rpm + steer))
        right_rpm = max(-MAX_RPM, min(MAX_RPM, base_rpm - steer))
        return left_rpm, right_rpm

    def _apply(self, left_rpm: float, right_rpm: float) -> None:
        """Send RPM commands to all motors. Right-side motors are negated to match mounting direction."""
        left = round(left_rpm)
        right = round(right_rpm)
        for motor_id in LEFT_SIDE:
            self.motor.send_rpm(motor_id, rpm=left)
        for motor_id in RIGHT_SIDE:
            self.motor.send_rpm(motor_id, rpm=right * (-1))

    def _brake(self) -> None:
        """Apply hardware brake to all motors. No-op if already braked."""
        if not self._braked:
            print_warning("BRAKE")
            for motor_id in LEFT_SIDE + RIGHT_SIDE:
                self.motor.set_brake(motor_id)
            self._current_rpm = 0.0
            self._braked = True

    def _stop_motors(self) -> None:
        """Send rpm=0 to all motors. Called on shutdown."""
        for motor_id in LEFT_SIDE + RIGHT_SIDE:
            self.motor.send_rpm(motor_id, rpm=0)

    @staticmethod
    def _ramp_toward(current: float, target: float) -> float:
        """Step current toward target by at most RAMP_STEP; snap when close enough."""
        diff = target - current
        if abs(diff) <= RAMP_STEP:
            return target
        return current + RAMP_STEP * (1 if diff > 0 else -1)
