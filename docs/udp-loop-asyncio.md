# The UDP control loop on asyncio: decisions

The gamepad control loop in `apps/vehicle_control/` now runs on asyncio. It
waits on a `DatagramProtocol` instead of polling the socket every 50 ms with
`time.sleep`, and writes to the motors from a worker thread.

## Why

- **Practice before the merge.** This was item 1 of the backlog's async track.
  Backlog item "One async process runs everything" will put this loop, the API,
  the FC reader and telemetry in one event loop, so the loop has to be a
  well-behaved coroutine first.
- **The blocking bus.** `DDS115.send_rpm()` blocks the caller. It writes a
  frame, then waits for the motor's reply, and `ser.read(1)` waits up to 100 ms
  when a motor does not answer. On the event loop, that stalls everything else
  in the process.

## Measurement

Measured on the laptop with `PYTHONASYNCIODEBUG=1`, which warns about any step
longer than 100 ms. The fake bus used realistic latencies. A 10 ms heartbeat
task shows how late everything else in the process gets to run. Numbers are
for 2s of stick forward at 20 Hz.

| Bus | Motor calls | Slow-step warnings | Longest step | Worst heartbeat delay |
| --- | --- | --- | --- | --- |
| motors answering, ~5 ms each | on the loop | 0 | under 100 ms | 21 ms |
| motors answering, ~5 ms each | `to_thread` | 0 | - | 2 ms |
| one motor silent, 100 ms each | on the loop | 41 | 402 ms | 393 ms |
| one motor silent, 100 ms each | `to_thread` | 0 | - | 2 ms |

With a silent motor, a pass over the four motors made on the loop is a
400 ms step, close to the 0.5s link timeout. In a worker thread, other work
waits about 2 ms, whatever the bus does.

On the Pi, run `PYTHONASYNCIODEBUG=1 ./start_vehicle.sh` with one motor
unpowered to confirm: there should be no `Executing ... took` warnings.

## Decisions

| Decision | Why | Rejected |
| --- | --- | --- |
| `asyncio.to_thread` for the bus | `DDS115` stays as it is, shared with the API, and one pass at a time needs one thread. | `pyserial-asyncio` (BSD): means rewriting `DDS115`'s half-duplex write and sliding-window reply parser as coroutines, plus a new dependency on the Pi, for no gain over one thread. |
| Protocol keeps only the newest packet | Replaces the old "drain the socket" loop. Packets that land while a pass is on the bus collapse to one, so the rover never acts on a position the driver has left. | Queueing every packet. |
| Act per packet, not on a 20 Hz tick | Nothing happens between packets anyway, and the controller already sends at 20 Hz; each packet is one ramp step, as before. | Keeping a fixed tick on `asyncio.sleep`. |
| `asyncio.timeout_at(last arrival + 0.5s)` | Time spent on the bus counts as silence, as it did with the old timestamp check. A timeout started when the wait began would give a silent link up to 0.5s extra. | `asyncio.timeout(0.5)` around each wait. |
| Arrival stamped in `datagram_received` | The loop stamps packets the moment they land, even while a pass is on the bus. The laptop's `timestamp` is still ignored, since its clock need not match the Pi's. | Stamping when the loop takes the packet. |
| One lock around every pass | A thread cannot be cancelled, so on Ctrl+C a pass may still be writing. The final stop waits on the lock and runs after it, not between its frames. | Stopping straight away from the cancelled task. |
| The same safety rules, unchanged | No stop before the first packet, one stop per outage, "press A again" after a drop. Only the clock that detects the silence changed. | - |
| `UDPReceiver` replaced by `ControllerStateProtocol` + `parse_packet` | Nothing polls any more. Bad packets (bad JSON or UTF-8, missing or extra fields) are dropped and do not count as signs of life. | Keeping the polling class next to the protocol. |

## Verification

- 13 controller tests in `tests/test_vehicle_controller.py`, all timing-based
  on the event loop:
    - the link-drop rules above;
    - bus time counting as silence;
    - newest-wins during a slow pass;
    - Ctrl+C mid-pass ending with rpm 0;
    - bad packets;
    - a real UDP socket;
    - a debug-mode run with a 100 ms-per-motor bus that must log no slow steps.
- End to end on the laptop: `apps.vehicle_control.main` with a fake motor and
  packets from the real `UDPSender`. The motors ramped to 100 rpm, and Ctrl+C
  sent rpm 0 to all four motors, then closed the port.
- Not yet run on the Pi or real motors.
