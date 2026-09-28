# Reading the flight controller as a stream: decisions

The API now keeps one MAVLink link to the flight controller open and takes
every `ATTITUDE`, `SYS_STATUS` and `HEARTBEAT` as it arrives
(`apps/api/services/flight_controller.py`). The 2s status probe no longer
touches the FC.

## Why

- **Stale attitude.** The probe reopened the link every 2s, waited for a
  heartbeat and read for 0.6s. Attitude on the status page was up to 2s old,
  and the horizon moved in jumps.
- **Cost.** Every probe paid for the open and the heartbeat wait, to learn what
  an open link reports for free.
- **Practice before the merge.** This was item 1 of the backlog's async track.
  Backlog item "One async process runs everything" needs an FC reader that
  lives on the event loop.

## How it works

```
 FlightControllerStream._run                      (a task on the API's event loop)
   loop:
     open link, wait for heartbeat   ── to_thread ──> pymavlink
     read until lost:
       recv_match(timeout 0.25s)     ── to_thread ──> pymavlink
       HEARTBEAT -> connected, last_heartbeat = now
       ATTITUDE / SYS_STATUS -> new FcReading (swapped whole)
       no heartbeat for 3s -> lost
     close link (after any read still in its thread)
     readings -> null; sleep backoff 0.5s, 1s, 2s ... 10s

 VehicleStatusService.snapshot()  reads stream.reading()   (any thread)
```

## Decisions

| Decision | Why | Rejected |
| --- | --- | --- |
| Each pymavlink call in `to_thread`, with 0.25s read timeouts | pymavlink blocks. Short reads keep shutdown prompt: 0.2s measured on SIGTERM. | A dedicated reader thread: loses the lesson and the event-loop ownership that the one-process item needs. |
| Lost = a failed call **or** 3s without a heartbeat | USB serial raises when the cable comes out; a UART just goes quiet. The FC sends a heartbeat every second, so 3s is three missed. | Failed calls only: a silent UART would read as connected forever. |
| Readings go `null` the moment the link is lost | Kept from the probe: a value from a dead link would read as current. | Keeping the last known values. |
| Backoff 0.5s doubling to 10s, reset after a link that worked | An unplugged FC costs a retry every 10s, not a busy loop. A cable pushed back in reconnects within 10s. | A fixed retry interval. |
| Reading published as one frozen `FcReading`, swapped whole | `snapshot()` is called from the event loop, the /status threadpool and the telemetry thread. Swapping one reference is atomic, so no lock is needed and no reader sees half an update. | A lock around mutable dicts. |
| Close waits for the read still in its thread | A thread cannot be cancelled; closing the port under a live `recv_match` would fail inside it. Same pattern as `DriveSession` arming. | Closing straight away on cancel. |
| No `asyncio.Event` for "new reading" yet | The backlog suggested one, but nothing would wait on it today. The 5 Hz broadcaster samples the latest reading, and telemetry samples every 5s. Add it with its first consumer. | An Event with no consumer. |
| Motor bus and Pi health stay in the probe thread | They are separate items; this change only takes the FC out of the probe. | Moving the whole probe to asyncio at once. |
| `ComponentSnapshot` moved to `services/components.py` | The stream and the status service both need it, and importing it from `vehicle_status` would be circular. | - |

## Verification

- 16 tests in `tests/test_flight_controller.py` with a fake link whose
  `recv_match` blocks like pymavlink's:
    - unit conversions and the "unknown" sentinels;
    - live attitude;
    - the battery kept between `SYS_STATUS` messages;
    - a bad frame costing the reading, not the link;
    - a port that opens but never speaks;
    - a link gone quiet, then reopened;
    - a pulled cable reconnecting;
    - backoff doubling and capping, and starting over after a good link;
    - stop waiting for the read in flight, and stopping promptly;
    - unconfigured;
    - the stream seen through `VehicleStatusService.snapshot()`.
- The real app with real pymavlink, pointed at `FC_DEVICE=udpin:127.0.0.1:…`,
  with a script sending the FC's heartbeat (1 Hz), `ATTITUDE` (20 Hz) and
  `SYS_STATUS` (2 Hz) over UDP:
    - `/status` polled every 0.25s showed roll moving each time;
    - with the script stopped, the FC went down about 3s later, reading "No
      heartbeat for 3s; retrying in 0.5s", with readings `null`;
    - with the script restarted, it reconnected on its own;
    - SIGTERM shut down in 0.2s;
    - under `PYTHONASYNCIODEBUG=1`, no slow steps from the stream. The one
      warning, 160-180 ms during startup, happens with the FC unconfigured too.
- Not yet run against the real flight controller on the Pi.
