# Vehicle control with gamepad

This project uses a **split control setup**:

1. The **Raspberry Pi on the vehicle** runs the motor control loop.
2. A **laptop or another computer** reads the USB/Bluetooth gamepad.
3. The laptop sends controller state over **UDP** to the Raspberry Pi.

## How it works

### Vehicle side

Run on the Raspberry Pi:

```bash
./start_vehicle.sh
```

That starts `python -m apps.vehicle_control.main`, which:

1. Opens the DDS115 motor bus.
2. Starts a UDP receiver on port `5005` - an asyncio `DatagramProtocol`, so each
   packet is handled as it lands rather than on a 20 Hz poll.
3. Converts incoming gamepad state into left/right motor RPM commands. The
   motor writes run in a worker thread, so a slow or silent motor never stalls
   the event loop; packets that land meanwhile collapse to the newest one.

### Controller side

Run on the laptop with the gamepad connected:

```bash
./start_controller.sh
```

That starts `python -m apps.controller.gamepad_main`, which:

1. Detects the first connected joystick with `pygame`.
2. Reads sticks and buttons at about **20 Hz**.
3. Sends the current state over UDP to `raspberrypi.local:5005`.

If your Raspberry Pi is not reachable as `raspberrypi.local`, update `UDP_HOST` in `apps/controller/gamepad_main.py`.

## Current control mapping

### Safety / mode

1. **A button**: toggle drive mode on or off.
2. **LB button**: apply brake to all motors and disable drive mode.
3. **Link-drop stop**: if no packet arrives for 0.5s (Wi-Fi drop, laptop asleep,
   controller process killed), the vehicle stops all motors and turns drive mode
   off. When packets resume, press **A** again to drive - a stick still held
   forward will not move the rover on its own.

### Driving

1. **Left stick Y**: forward/backward speed.
   - stick up -> forward
   - stick down -> backward
2. **Right stick X**: steering.
   - when moving, it applies differential steering
   - when stopped, it creates a tank turn

## Typical startup flow

1. Power the vehicle and make sure the Raspberry Pi is up.
2. SSH into the Raspberry Pi if you want to watch logs.
3. Start the vehicle process with `./start_vehicle.sh`.
4. Connect the gamepad to the laptop.
5. Start the controller process with `./start_controller.sh`.
6. Press **A** once to enable drive mode.
7. Move with the sticks.
8. Press **LB** any time for an immediate brake.

## Operational notes

1. The vehicle does **not** move until drive mode is enabled with **A**.
2. When drive mode is turned off, RPM ramps back to zero gradually.
3. The control loop ignores small stick jitter near center with a dead zone.
4. If the laptop loses network connectivity, UDP packets stop arriving and the vehicle will no longer receive fresh control updates.
5. `Ctrl+C` stops either side; on vehicle shutdown the motor controller sends `rpm=0` to all motors.
