import asyncio
from typing import Optional

from lib.common.formatting import print_info, print_warning
from lib.gamepad.udp_receiver import ControllerStateProtocol
from lib.gamepad.state import ControllerState
from lib.ddsm115 import MotorBus

# Who holds the bus, in what another driver is told: "... is driving".
DRIVER = "The gamepad"
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
    socket on a timer. It drives through the rover's one `MotorBus`, which it
    shares with the status page and the /motors ramps, so it holds the bus
    only while drive is on: pressing 'a' claims it, and turning drive off,
    braking or losing the link releases it. While it does not hold the bus it
    sends nothing - someone else may be driving.

    Control scheme:
        - Press 'a' to toggle drive mode on/off. Refused, with a warning,
          while someone else is driving.
        - Left joystick Y  → forward (up) / backward (down), RPM ramps smoothly.
        - Right joystick X → steering; blends with base RPM so the same input
          produces differential steering while moving and a tank turn when stopped.
        - 'lb' button      → brake all motors and disable drive mode.
        - No packet for LINK_TIMEOUT → stop all motors and disable drive mode;
          'a' must be pressed again once the link is back.

    Motor wiring convention (from actuator_test.py):
        RIGHT_SIDE motors receive rpm * -1 to match the physical mounting
        direction; the bus applies it.
    """

    def __init__(self, bus: MotorBus, link_timeout: float = LINK_TIMEOUT):
        self.bus = bus
        self.link_timeout = link_timeout
        self._current_rpm: float = 0.0
        self._drive_enabled: bool = False
        # Holds the bus: from 'a' until drive is off and the motors have
        # ramped down to zero, or a brake or the fail-safe let it go.
        self._holding: bool = False
        self._prev_a: bool = False  # for edge detection on 'a' toggle
        self._link_lost: bool = False

    async def run(self, packets: ControllerStateProtocol) -> None:
        """Act on packets as they arrive until cancelled, then stop the motors."""
        print_info("VehicleController started")
        try:
            while True:
                try:
                    async with asyncio.timeout_at(self._link_deadline(packets)):
                        state = await packets.next_state()
                except TimeoutError:
                    await self._fail_safe()
                    continue
                if self._link_lost:
                    self._link_lost = False
                    print_info("Control link restored - press 'a' to drive")
                # Packets that land while this pass is on the bus are not
                # queued: the protocol keeps the newest, which is taken next.
                try:
                    await self._handle(state)
                except RuntimeError as exc:
                    # The bus failed or was taken away under us.
                    print_warning(f"Motor bus failed: {exc} - drive disabled")
                    await self._let_go()
        finally:
            # Owner-checked: a no-op unless the gamepad holds the bus.
            await self.bus.release(self)

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _handle(self, state: ControllerState) -> None:
        """Process one controller state snapshot and update motor outputs."""
        a_now = state.buttons.a
        if a_now and not self._prev_a:
            if self._drive_enabled:
                self._drive_enabled = False
                print_info("Drive DISABLED")
            else:
                self._enable()
        self._prev_a = a_now

        print(
            f"drive={self._drive_enabled} lb={state.buttons.lb} "
            f"left_y={state.axes.left_y:+.2f} right_x={state.axes.right_x:+.2f} "
            f"rpm={self._current_rpm:.0f}"
        )

        if not self._holding:
            return

        if state.buttons.lb:
            await self._brake()
            return

        if self._drive_enabled:
            left_y = state.axes.left_y  # left joystick up/down: up = -1, down = +1
            right_x = state.axes.right_x  # right joystick left/right: left = -1, right = +1

            # Apply dead zone to forward/back so releasing the stick ramps to 0 smoothly
            target_rpm = 0.0 if abs(left_y) < DEAD_ZONE else -left_y * MAX_RPM
            self._current_rpm = self._ramp_toward(self._current_rpm, target_rpm)

            left_rpm, right_rpm = self._compute_side_rpms(self._current_rpm, right_x)
        else:
            # Drive off: ramp back to zero gradually, then let the bus go.
            self._current_rpm = self._ramp_toward(self._current_rpm, 0.0)
            left_rpm = right_rpm = self._current_rpm

        await self.bus.drive(self, round(left_rpm), round(right_rpm))
        if not self._drive_enabled and self._current_rpm == 0.0:
            await self._let_go()

    def _enable(self) -> None:
        try:
            self.bus.claim(self, DRIVER)
        except RuntimeError as exc:
            print_warning(f"Cannot drive: {exc}")
            return
        self._drive_enabled = True
        self._holding = True
        print_info("Drive ENABLED")

    async def _fail_safe(self) -> None:
        """Stop the motors and disarm once the control link has gone quiet.

        The motors hold their last commanded RPM, so without this a laptop that
        sleeps or a Wi-Fi drop mid-drive leaves the rover going at full speed.
        Stops at once rather than ramping: a ramp from full speed takes seconds
        of driving blind. Drive stays off until 'a' is pressed again, so a link
        that comes back with the stick still pushed does not lurch forward.
        """
        self._link_lost = True
        # A held 'a' must be released and pressed again, not read as a fresh press.
        self._prev_a = True
        if self._holding:
            print_warning(f"Control link lost for >{self.link_timeout:.1f}s - stopping motors")
        await self._let_go()

    async def _brake(self) -> None:
        """Apply hardware brake to all motors, then disable drive."""
        print_warning("BRAKE")
        try:
            await self.bus.brake(self)
        finally:
            await self._let_go(stop=False)

    async def _let_go(self, *, stop: bool = True) -> None:
        """Drive off, and the bus released - with a stop unless `stop` is
        False (after a brake, which already stopped them)."""
        self._drive_enabled = False
        self._holding = False
        self._current_rpm = 0.0
        await self.bus.release(self, stop=stop)

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

    @staticmethod
    def _ramp_toward(current: float, target: float) -> float:
        """Step current toward target by at most RAMP_STEP; snap when close enough."""
        diff = target - current
        if abs(diff) <= RAMP_STEP:
            return target
        return current + RAMP_STEP * (1 if diff > 0 else -1)
