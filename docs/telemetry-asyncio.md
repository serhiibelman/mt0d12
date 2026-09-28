# The telemetry publisher on asyncio: decisions

`TelemetryPublisher` now runs on the API's event loop. MQTT goes through
`aiomqtt` and the offline spool through `aiosqlite`, replacing the paho network
thread, the publisher thread and its `time.sleep` loop. The decision logic is
unchanged: what counts as a change, the heartbeat and idle heartbeat, the
spool-first order, the fallbacks. So are the spool's file format and the
payload.

## Why

- **Stalls.** The threaded publisher sent inline: an outage or a half-open
  connection held its loop for the whole timeout on every tick. Every tick
  retried, so a long outage cost an attempt every 5s, forever.
- **Draining.** Draining a backlog and taking new samples happened on the same
  thread, one after the other.
- **Practice before the merge.** This was item 1 of the backlog's async track.
  Backlog item "One async process runs everything" needs a publisher that is a
  coroutine, not a thread.

## How it works

```
 sampler task (every TELEMETRY_INTERVAL_SECONDS)
   snapshot -> due? (change / heartbeat / idle) -> spool.append  (local write only)
   wake the drainer                                               never waits on the network

 drainer task
   wait for a wake
   drain: spool.pending(batch) -> publish each (asyncio.timeout) -> spool.discard(acked)
   emptied  -> wait for the next wake
   failed   -> sleep 1s, 2s, 4s ... 60s, then try again (samples keep spooling)
```

`publish_once`, `publish_if_due` and `flush` are still there, now `async`, for
callers that want an answer: `telemetry_test.py` and the tests.

## Decisions

| Decision | Why | Rejected |
| --- | --- | --- |
| Two tasks: a sampler and a drainer | Sampling is a local write and never waits on the network. A slow drain or an outage cannot delay or drop a sample. | One loop that samples, then sends: the old shape, where the uplink set the pace. |
| Retry backoff 1s doubling to 60s, reset once the spool empties | An outage costs one attempt a minute instead of one per 5s tick. A link that comes back is used within a minute. | Retrying every tick. |
| `asyncio.timeout` around connect, publish and disconnect | A half-open connection (broker paused, Wi-Fi gone) never answers. Each call gives up after `TELEMETRY_PUBLISH_TIMEOUT_SECONDS`. | Relying on the library's own timeouts. aiomqtt has them, but the connect runs in an executor and the disconnect waits for an acknowledgement. |
| Acknowledged ids discarded in a `finally`, shielded | A stop that lands mid-batch still forgets what the broker has taken. What stays on disk is exactly what has not gone out, so a restart sends nothing twice. | Discarding per batch only on success. A cut-off batch would be resent whole. |
| No final drain on stop | With the uplink down it would hold up shutdown for a timeout, to deliver nothing. The spool already keeps everything. | Draining for a few seconds on SIGTERM. |
| Spool on `aiosqlite`, same schema and version | The SD card's fsync (`synchronous=FULL`) runs on aiosqlite's thread, not the event loop. An existing spool on the Pi carries over. | Calling `sqlite3` from the loop; a schema bump that would discard the Pi's backlog. |
| An `asyncio.Lock` in the spool | Each call is several statements and a commit. Interleaved, one call's commit would take another's half-done work. | - |
| aiomqtt 2.5.1 (BSD), aiosqlite 0.22.1 (MIT) | Permissive licences. Both pure Python with no dependencies beyond paho 2.1 (already pinned), so both install on the Pi 1 (armv6) as they are. | `awsiotsdk`: its C extension has no 32-bit ARM wheel. |
| One new aiomqtt client per connection | Simpler than reusing one across failures, and a connection lives for many publishes anyway. | Reconnecting the same client. |

Delivery is still at-least-once. A publish that times out may still have
reached the broker, and then the retry delivers it a second time. The
end-to-end run below shows exactly that: one duplicate after the broker was
paused. The backlog's base-station item already covers it: upsert on
`(thing_name, recorded_at)`.

## Verification

- 48 tests in `tests/test_telemetry.py`:
    - every threaded-version test, kept as the specification, with `await`
      added;
    - aiomqtt adapter tests: mutual TLS, a persistent session, QoS 1 with a
      timeout;
    - the background tasks publishing on change;
    - backoff doubling to its cap;
    - a parked rover draining once the link is back;
    - connects, publishes and disconnects that never answer, each timing out;
    - a 10-message backlog draining while 5 new samples are spooled, in order,
      nothing twice;
    - a stop mid-drain: every sample either delivered or on disk, never both.
- 13 spool tests, ported to async with the same assertions.
- A real Mosquitto 2 broker in Docker, requiring client certificates as AWS IoT
  does:
    - live changes arrived at once;
    - with the broker paused (connection half-open), the publish timed out
      after 1s and the next samples spooled;
    - after unpausing, the backlog drained in order;
    - with the broker killed and restarted, samples spooled, then were
      delivered after reconnecting;
    - `stop()` took 0.01s;
    - all of 1-10 arrived in order, with the one duplicate described above.
- Not yet run against AWS IoT Core from the Pi. `python telemetry_test.py` is
  the check.
