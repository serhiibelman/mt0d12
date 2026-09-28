import asyncio
import threading
from typing import Any, Callable, Optional

from lib.common.formatting import print_info, print_warning
from lib.gamepad.udp_receiver import ControllerStateProtocol
from lib.gamepad.state import ControllerState
from lib.ddsm115 import DDS115
from settings import RIGHT_SIDE, LEFT_SIDE

MAX_RPM = 200
MAX_STEER_RPM = 100  # half of MAX_RPM for gentler turns
RAMP_STEP = 5
DEAD_ZONE = 0.1  # ignore axis jitter near center
# The /motors/start ramp's step. The control loop no longer ticks: it acts on
# each packet as it lands, and the controller sends at 20 Hz.
LOOP_INTERVAL = 0.05
# The controller sends at 20 Hz whether or not the sticks move, so this much
# silence is ~10 lost packets in a row: the link is gone, not just lossy.
LINK_TIMEOUT = 0.5


class VehicleController:
    """
    Controls vehicle motors based on gamepad input received over UDP.

    Runs on asyncio: `run` waits on the UDP protocol instead of polling the
    socket on a timer. The motor bus blocks - each `send_rpm` waits for the
    motor's reply, up to 100 ms for one that does not answer - so every pass
    over the motors runs in a worker thread, and the event loop never stalls
    on the serial port.

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

    def __init__(self, motor: DDS115, link_timeout: float = LINK_TIMEOUT):
        self.motor = motor
        self.link_timeout = link_timeout
        self._current_rpm: float = 0.0
        self._braked: bool = False
        self._drive_enabled: bool = False
        self._prev_a: bool = False  # for edge detection on 'a' toggle
        self._link_lost: bool = False
        # One pass over the motors at a time. A worker thread cannot be
        # cancelled, so on shutdown the final stop waits here for a pass still
        # on the bus rather than writing over it.
        self._bus_lock = threading.Lock()

    async def run(self, packets: ControllerStateProtocol) -> None:
        """Act on packets as they arrive until cancelled, then stop the motors."""
        print_info("VehicleController started")
        try:
            while True:
                try:
                    async with asyncio.timeout_at(self._link_deadline(packets)):
                        state = await packets.next_state()
                except TimeoutError:
                    await self._on_bus(self._fail_safe)
                    continue
                if self._link_lost:
                    self._link_lost = False
                    print_info("Control link restored - press 'a' to drive")
                # Packets that land while this pass is on the bus are not
                # queued: the protocol keeps the newest, which is taken next.
                await self._on_bus(self._handle, state)
        finally:
            await self._on_bus(self._stop_motors)

    def _link_deadline(self, packets: ControllerStateProtocol) -> Optional[float]:
        """When silence becomes a lost link, in loop time; None for no limit.

        Counted from the last packet's arrival, not from when the wait began,
        so time spent on the bus counts as silence too. No limit before the
        first packet - until a controller has spoken the motors have not been
        commanded, so there is nothing to stop - or once the link is already
        lost, so the stop is sent once per outage.
        """
        if self._link_lost or packets.last_arrival is None:
            return None
        return packets.last_arrival + self.link_timeout

    async def _on_bus(self, action: Callable[..., None], *args: Any) -> None:
        """Run `action` in a worker thread, holding the bus."""

        def locked() -> None:
            with self._bus_lock:
                action(*args)

        await asyncio.to_thread(locked)

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

    @staticmethod
    def _compute_side_rpms(base_rpm: float, right_x: float) -> tuple[float, float]:
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
