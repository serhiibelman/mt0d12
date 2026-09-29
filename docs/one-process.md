# One async process runs the rover: decisions

The vehicle runs one program now, `python -m apps.rover.main`
(`./start_rover.sh`). It replaces the API under `uvicorn` and
`apps.vehicle_control.main`. One event loop runs the motor bus, the flight
controller stream, the Pi probe, the camera, telemetry, the gamepad link and
the web server, each as a task under a supervisor. The status probe and the
camera moved off their threads on the way.

## Why

- **Two processes, one serial port.** The API and `vehicle_control` both
  opened the motor bus, and whichever started second got a 503. So it was
  either the status page or the gamepad, never both.
- **Telemetry only while the API ran.** Driving with the gamepad alone
  published nothing.
- **One crash took everything in its process down with it.** Nothing
  restarted a part that failed.
- **Practice.** This was item 3 of the backlog's async track: structured
  concurrency across a whole application, priorities, and shutdown order.

## How it works

```
 apps/rover  (one event loop)
 ────────────────────────────────────────────────────────────────────────
 supervise("motor bus")          MotorBus.run      port open, watched, reopened
 supervise("flight controller")  FC stream         pymavlink reads in to_thread
 supervise("status probe")       Pi health         every 2s; vcgencmd as a subprocess
 supervise("telemetry")          sampler + drainer (returns at once if unconfigured)
 supervise("gamepad")            UDP 5005 -> VehicleController
 supervise("web server")         uvicorn.Server.serve() -> /status, /ws/status, /camera
 camera                          opened by the first viewer, closed 2s after the last

 who drives:   status page ─┐
               gamepad     ─┼─ claim ─> MotorBus ── own thread ──> DDS115 port
               /motors ramp ┘   release (stops the motors)
```

Stopping, on SIGINT or SIGTERM:

1. `MotorBus.halt()`: the motors stop, and every later claim is refused.
2. The gamepad link is cancelled.
3. The camera closes, which ends every MJPEG stream.
4. The web server closes its connections, then exits. It is cancelled if it
   takes longer than 5s.
5. Telemetry, the probe and the FC stream are cancelled. Telemetry's spool
   keeps whatever has not gone out yet.
6. The bus sends one more stop and closes the port.

Where the code lives:

| File | Role |
| --- | --- |
| `apps/rover/main.py` | Builds the parts, takes the signals |
| `apps/rover/rover.py` | `Rover`: runs the parts and stops them in order. `ApiServer`: uvicorn as a task |
| `apps/rover/supervisor.py` | `supervise`: restarts a part that raises |
| `lib/ddsm115/bus.py` | `MotorBus`: the one owner of the motor port |
| `apps/api/services/camera.py` | `CameraService`, on the event loop |
| `apps/api/services/vehicle_status.py` | The snapshot, the probe task and the `/motors` ramps |

## Decisions

| Decision | Why | Rejected |
| --- | --- | --- |
| The motor port opens once and stays open, owned by `MotorBus` | A driver arming no longer waits on a port open. That wait is what used to lose the drive link while the camera held the Pi 1's core. The probe no longer opens the port every 2s to see whether it can. | Opening per session and per ramp, as before. That arbitration happened in the OS, and the loser got a 503. |
| One holder at a time: `claim`, then `release`, which stops the motors | The page, the gamepad and a ramp can never mix commands on the wire. A refusal names who is driving: "The gamepad is driving". The owner check makes a stale session a no-op. | Last command wins, which would let two drivers fight over the motors. Gamepad always wins, which would surprise whoever holds the page. |
| The bus runs every port call on its own single worker thread | The calls are serialised without a lock and stay in the order they were made, so a stop always comes after the pass it follows. The shared `to_thread` pool also serves FC reads, the camera and the spool, and a drive command never queues behind them. This is the priority mechanism: driving never waits for telemetry. | The default pool plus a `threading.Lock`. With the pool busy, a drive command waited for a free worker. |
| Port calls are shielded | Cancelling an `run_in_executor` future that has not started drops the call. The stop sent on the way out of a cancelled task is exactly that call. | Unshielded calls, which lose the final stop exactly when it matters. |
| A lost port is noticed two ways: a command that fails, or the device node gone while nobody drives. It reopens with backoff, 0.5s doubling to 10s | The same shape as the FC stream. An idle USB adapter raises nothing until it is written to. | Pinging the motors while idle. That would send commands to motors a `/motors/start` left turning. |
| The gamepad holds the bus only while drive mode is on | Unclaimed, it sends nothing, so it cannot fight a page that is driving. 'a' is refused while someone else holds the bus. Drive off ramps to zero, then releases. | The gamepad sending every packet as before. It would overwrite the page's commands. |
| `/motors/start` claims, ramps and releases without a stop | The motors keep turning after the ramp, as the endpoint always did. The next ramp starts from what was last sent. | Keeping a hold after the ramp. Nobody would ever release it. |
| `supervise`: restart on `Exception`, backoff 1s doubling to 30s, reset after 60s healthy. A part that returns stays finished | One bug costs one part. A part that crashes in a loop costs one attempt every 30s. Telemetry with no endpoint returns, so it is not restarted. | A `TaskGroup` over the parts. The first crash would cancel the driving too. |
| A part that fails while being cancelled is not restarted | `Task.cancelling()` tells a stop from a crash. Without that check, a `finally` that raised would restart telemetry during shutdown. | - |
| Ordered stop, motors first | Nothing that stops later can be what keeps the rover moving. The camera closes before the web server because an open MJPEG stream would otherwise hold the server up for the whole timeout. | Cancelling everything at once, which leaves the order to chance. |
| `ApiServer.stop` shuts connections down before telling uvicorn to exit | uvicorn 0.22 on Python 3.12+ waits for open connections before it asks them to close, so an open status page held SIGTERM up forever. Closing them first fixes that; the page gets 1012. A 5s timeout, then cancel, is the backstop. | Upgrading uvicorn in the same change. That is still on the backlog, with the Pi checks it needs. |
| uvicorn's own signal handlers are switched off | The rover takes SIGINT and SIGTERM itself, so it can stop in its own order. | Letting uvicorn exit first, which leaves the rest in an undefined state. |
| A bind failure (`SystemExit` from uvicorn) becomes a crash to retry | After a restart, the port may still be held by the process on its way out. | Letting `SystemExit` end the rover. |
| The camera is loop-owned: frames cross with `call_soon_threadsafe`, opening and closing run in `to_thread` under an `asyncio.Lock`, and the idle stop is a task | No thread locks and no `threading.Timer`. Waiting for a frame is a plain `Event`, with no lost wake-up to reason about. | Keeping the `Condition` and the `RLock` next to an asyncio viewer path, which is two concurrency models in one class. |
| A generation number on the camera's frames | picamera2 can hand over one last frame after `stop_recording`. Without the number it would show up as the first frame of the next open. | - |
| One camera open shared by every waiter, and shielded | Three viewers arriving together open the sensor once. A viewer that leaves mid-open does not strand it: the open finishes and the idle stop closes it. | Letting cancellation interrupt the open, which leaves an open sensor that nothing knows about. |
| `/camera/snapshot` takes a viewer slot while it waits | A snapshot used to open the camera and leave it on until `/camera/stop`. Now the idle stop closes it again. | - |
| The Pi probe: `vcgencmd` via `asyncio.create_subprocess_exec` with a timeout that kills it. sysfs and procfs reads stay inline | Only the subprocess can take real time. The file reads are answered from kernel memory in microseconds, and a thread hop would cost more than the read. | `to_thread` around the whole reader. |
| Every route that reads state is `async def` | The snapshot is then only ever read on the loop, so no state needs a thread lock. | Sync routes in the threadpool, which need a lock around every read. |
| `create_app` builds and starts nothing | The rover owns the services and their lifetimes. The app only serves them. The tests hand it fakes. | The app's lifespan starting the services, which is how two owners happened in the first place. |
| `start_api.sh`, `start_vehicle.sh` and `apps/vehicle_control/main.py` removed | Two entry points would bring the two port owners back. | Keeping them for development. `python -m apps.rover.main` runs on the laptop too, since every part copes with missing hardware. |

## Verification

- 200 tests pass; there were 176. The service's per-session port tests moved
  to the bus, and everything else was ported to the async API with its
  assertions kept.
    - `tests/test_motor_bus.py` (12):
        - drivers take turns on one open port;
        - one holder at a time, and the refusal says who;
        - a stale session cannot touch the next one's bus;
        - a ramp's release without a stop;
        - `halt`;
        - stopping the bus stops the motors and closes the port;
        - a failed command closes the port and it reopens;
        - an adapter unplugged while idle is noticed;
        - backoff on a port that will not open;
        - unconfigured;
        - a drive command goes out in under 0.2s while every worker in
          asyncio's default pool is busy for 1s;
        - a stop from a cancelled task still goes out after the pass in
          flight.
    - `tests/test_rover.py` (8):
        - supervisor backoff doubling and capped;
        - a part that returns stays finished;
        - a healthy run resets the backoff;
        - a failure during cancellation is not restarted;
        - the shutdown order, checked step by step;
        - a crashing part costs only itself;
        - the web server stops in under 1s with a WebSocket open, and the
          page gets 1012. With the fix taken out, this test fails at its
          timeout rather than passing;
        - a bind failure is a crash to retry.
    - Camera, 15 tests to 20:
        - viewers arriving together share one open;
        - a failed open gives its slot back;
        - a late frame from a closed camera is dropped;
        - a viewer leaving mid-open does not leave the camera on;
        - a snapshot lets the camera close after.
    - Gamepad, 14 tests to 17:
        - 'a' is refused while the page drives;
        - drive off ramps down, then releases;
        - brake releases.
    - The event-loop stall test and its control still pass against the bus
      thread.
- The real process, `python -m apps.rover.main` on the laptop. A pty stood in
  for the RS-485 port, FC on an unused UDP port, camera off, no IoT:
    - the bus opened on the pty;
    - the gamepad over UDP 5005 armed and took the bus. The page's arm and
      `/motors/start` were then refused with "The gamepad is driving" (503
      for the ramp);
    - the gamepad going silent sent zeros to all four motors and released the
      bus, and the page could arm and drive;
    - SIGTERM with the page open and armed: the page got 1012, the last frames
      on the wire were zeros to all four motors, and the process exited 0
      after 1.4s;
    - before the pty's missing RTS line was stubbed out, every write failed
      with `ENOTTY`. That exercised the lost-port path for real: the bus
      closed, reported "Command failed: [Errno 25] ...; retrying in 0.5s",
      answered the ramp and the page with 503, and reopened.
- Not yet run on the Pi: the real DDS115 bus, the real FC, the camera, and
  shutdown timing on the Pi 1.
