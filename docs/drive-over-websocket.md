# Driving from the status page: decisions

The rover can be driven from the status page in a browser, over the same
`/ws/status` WebSocket that streams its status. This records why, and why it is
built the way it is.

## Why

Before this, driving needed the gamepad, a laptop running `gamepad_main` and
the UDP link to `vehicle_control`. A phone or laptop on the LAN could watch the
rover but not move it.

It was item 1 of the backlog's async track, which asked for:

- the page sending stick positions and the rover sending status back, on one
  socket;
- the listener task becoming the command reader, with cancellation taking
  every task down cleanly and the motors stopped, whichever side fails first;
- a link-drop stop: `asyncio.timeout(0.5)` around the receive, stopping the
  motors on expiry - `VehicleController`'s fail-safe as a timeout instead of a
  timestamp.

## How it works

Each `/ws/status` connection runs three tasks under one `asyncio.TaskGroup`:

```
 status page                 TaskGroup (one per connection)
 ───────────                 ──────────────────────────────
 renders status  <────────── _forward          broadcaster queue -> socket
 arm/drive/stop  ──────────> _read_commands    0.5s timeout while armed,
                                   │           keeps only the newest stick
                                   │ Event
                                   v
                             run_motors        ramp + mix -> to_thread ──> VehicleStatusService
                                                                           open_drive / drive / close_drive
```

Any task failing cancels the others; the route's `finally` then calls
`DriveSession.close`, which stops the motors.

Protocol:

| Direction | Message |
| --- | --- |
| page -> rover | `{"type": "arm"}` |
| page -> rover | `{"type": "drive", "throttle": -1..1, "steer": -1..1}`, every 50 ms while armed |
| page -> rover | `{"type": "stop"}` |
| rover -> page | `{"type": "drive", "armed": bool, "detail": str}` |
| rover -> page | the status snapshot, unchanged, with no `type` field |

Where the code lives:

| File | Role |
| --- | --- |
| `apps/api/services/drive.py` | `DriveSession`: arm, disarm, newest-wins command, motor loop, close |
| `apps/api/streaming.py` | `serve_until_disconnect`, `_read_commands`, the per-socket send lock |
| `apps/api/services/vehicle_status.py` | `open_drive` / `drive` / `close_drive`; the probe and `/motors/start` defer to a driver |
| `apps/api/static/status.html` | the Drive card: stick, W A S D / arrows, Space, Arm and Stop |

## Decisions

| Decision | Why | Rejected |
| --- | --- | --- |
| Same socket as `/ws/status` | One connection to open, reconnect and lose; the page already has backoff and dead-socket detection for it. | A separate `/ws/drive`: two links that fail independently. |
| Three tasks, not the two the backlog described | Each `send_rpm` waits for the motor's reply, so four motors can take longer than the 50 ms between commands. | The reader writing to the bus itself: commands queue up in the TCP buffer and the rover acts on stale positions. |
| Newest command wins | The rule `StatusBroadcaster` and the UDP loop's drain already follow; a position the driver has moved past is useless. | Applying every command in order. |
| Explicit `arm` and `stop` | Carries over the gamepad's "press A again": after a stop, a stick still held does not lurch the rover forward. | Arming on the first `drive` message. |
| Timeout only while armed | Watchers send nothing and must never be dropped. A driver sends at 20 Hz, so 0.5s of silence is ~10 lost commands. | A timeout on every connection, or app-level pings. |
| Stop at once, not ramped | A ramp down from 200 rpm is seconds of driving blind. Same as `VehicleController._fail_safe`. | Ramping down on a link drop. |
| Port held open per session | 20 commands a second cannot reopen the serial port each time, as `/motors/start` does. | Opening per command. |
| One driver, owner-checked | Two tabs would alternate commands. The owner check means an ended session can never command or close the next one's bus. | Last writer wins; a close with no owner check. |
| Bus calls in `asyncio.to_thread` | The serial writes block, and the event loop also serves status and every other request. | `pyserial-asyncio`: weighed again for the UDP loop and passed over there too; see [udp-loop-asyncio.md](udp-loop-asyncio.md). |
| Drive replies carry `type`, status does not | The page tells them apart without changing the schema `/status` and telemetry share. | A drive field in the status snapshot. |
| Per-socket send lock | Status and drive replies go out from different tasks; two interleaved frames would be one corrupt message. | Relying on one frame being one write today. |
| Mixing shared with `VehicleController` | The stick should feel the same on the page and the gamepad; `_compute_side_rpms` became static for it. | A second copy of the ramp and mix. |

## How every exit stops the motors

The stop runs in a worker thread, and a thread cannot be cancelled, so even a
cancelled session's stop finishes.

| Exit | What stops the motors |
| --- | --- |
| Stop button or Space | `disarm`: rpm 0 to all motors, port released, page told |
| No command for 0.5s (Wi-Fi drop, laptop asleep) | the reader's timeout expires, then `disarm`; the page must arm again |
| Tab hidden or closed | the socket closes, the group ends, the route's `finally` calls `DriveSession.close` |
| A send fails, or the motor bus errors | the group cancels the other tasks, then the `finally` as above; a bus error disarms first |
| Connection cut while the port is still opening | `close` waits for the open to finish, then releases the port it owns |
| API shutdown | the route's `finally`; `VehicleStatusService.stop()` also closes a driver still going |

A second tab's arm is refused ("Another viewer is driving"), `/motors/start`
answers 503 while someone drives, and the probe does not reopen a held port.

## Verification

- 147 tests pass (25 new) across `tests/test_drive_session.py`,
  `tests/test_vehicle_status_service.py` and `tests/test_api.py`: the ramp,
  newest-wins, refusals, the link-drop stop, leaving mid-drive, a second
  driver, and a port that finishes opening after its connection ended.
- Headless Chrome against the app with the suite's fake vehicle: holding up for
  1s ramps to 100 rpm, dragging right mixes the sides (160 / -40), Space and a
  hidden tab stop, and a 390px phone viewport fits.
- Not yet run on the Pi or real motors.

## Open questions

- Does 20 Hz of commands keep up on the Pi 1 while status streams at 5 Hz?
  Newest-wins makes the risk a slow ramp, not lag.
- Cancelling `receive()` under the pinned uvicorn 0.22 + websockets 13.1 - the
  timeout relies on it. It works on the laptop and websockets documents it as
  safe.
- Stop sends rpm 0, not the hardware brake the gamepad's LB uses. Should the
  page have a brake too?
