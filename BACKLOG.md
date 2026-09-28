# Backlog

Work that is understood but not done. Each item says why it matters and where
it lands, so picking one up does not mean rediscovering the problem.

Items 1-5 are an async-programming track and come first by choice: practice
on real code, with each step also moving the rover toward being driven from a
browser. Everything after them is ordered by what hurts most if it stays
undone, not by effort.

Repos: `mt0d12` (this one, the vehicle) and `mt0d12-infrastructure` (AWS).

---

## Async track

The vehicle runs on threads and `time.sleep` today: the control loop, the
telemetry publisher, the status probe and the camera each own one. Every item
below replaces one of them with its asyncio equivalent, in order of risk -
no new hardware until 4. Python 3.11+ (`TaskGroup`,
`asyncio.timeout`); the Pi's venv is 3.12. Build and test on the laptop with
fakes first - the suite already fakes motors, the FC link and clocks, and
`pytest-asyncio` extends that to coroutines.

### 1. Read the flight controller continuously

**Where:** `apps/api/services/vehicle_status.py` · **Size:** M

The probe reopens the MAVLink link every 2s and reads for 0.6s. Keep it open and
consume `ATTITUDE` / `SYS_STATUS` as a stream instead: pymavlink wrapped in
`to_thread`, an `asyncio.Event` for "new reading", and reconnect with backoff
when the cable comes out. Attitude becomes live rather than up to 2s old.

### 2. Async telemetry publisher

**Where:** `lib/telemetry/publisher.py` · **Size:** M

Rebuild `TelemetryPublisher` on `aiomqtt` (BSD) and `aiosqlite` (MIT) for the
spool: retries with exponential backoff, `asyncio.timeout` around every network
call, draining the backlog without blocking new samples, and a shutdown that
loses nothing. The threaded version and its tests are the specification.

### 3. Base station: a local broker and a recorder

**Where:** new `apps/base_station/`, `lib/telemetry/` · **Size:** M ·
**Needs:** a laptop or spare Pi that stays on

Telemetry reaches Postgres only through AWS. A base station keeps a copy on a
machine you own, reachable without the cloud: Mosquitto (EPL/EDL) as the
broker, and a small `aiomqtt` subscriber on `rover/+/telemetry` writing each
message to Postgres or SQLite - the other end of item 2.

The rover keeps publishing and keeps its spool; only the endpoint changes, so
the at-least-once delivery the outbox gives still holds. Two things to decide:

- **TLS.** The publisher always does mutual TLS, as AWS requires. Either give
  Mosquitto certificates the same way, or add a plain mode for a trusted LAN -
  never for anything leaving it.
- **Duplicates.** At-least-once means a replayed message can arrive twice; the
  recorder should upsert on `(thing_name, recorded_at)` rather than insert.

Mosquitto can bridge the same topics on to AWS, so this adds a copy rather
than replacing the cloud path. Recording stays on MQTT, not `/ws/status`: the
WebSocket is for a live view and loses whatever arrives while no one is
connected, and the rover connecting out to a fixed broker survives its own IP
changing - the station connecting in to the rover does not.

### 4. Ultrasonic sensors: threads into the event loop

**Where:** new `lib/` sensor module, `apps/vehicle_control/` · **Size:** M ·
**Needs:** 2-3 HC-SR04P

pigpio reports echoes by calling back on its own thread. Getting those readings
into the loop safely - `loop.call_soon_threadsafe()`, never touching loop state
from the callback - is the classic bug source this item is for. The payoff is
an obstacle stop, and the distance data any later mapping needs. pigpio is
public domain, `gpiozero` BSD. Power the HC-SR04P from 3.3V so its echo is
safe for the Pi's GPIO without a divider.

### 5. One async process runs everything

**Where:** new `apps/rover/` · **Size:** L

A single asyncio program owning the control loop, sensors, FC reader, telemetry
and the API, under a supervisor that restarts a task that crashes. Teaches
structured concurrency across a whole application, priorities (driving never
waits for telemetry) and shutdown order.

Also the architectural fix: today the API and `vehicle_control` both open the
motor serial port and whichever starts second gets a 503, and telemetry only
publishes while the API runs. One process owning the bus ends both.

---

## 6. Last Will and Testament

**Where:** `lib/telemetry/publisher.py` (`_PahoConnection.connect`) · **Size:** XS

Register an "offline" message at connect time and the broker publishes it the
instant the connection drops - including on power loss, where the Pi gets no
chance to say anything. Today offline is inferred from missing heartbeats,
which with the 300s idle interval means up to five minutes of ambiguity.

`client.will_set(topic=f"rover/{thing}/status", payload="offline", retain=True)`,
plus publishing `online` after connect. Retained, so anything subscribing later
sees current state immediately.

## 7. Scope the IoT policy

**Where:** `mt0d12-infrastructure/terraform/iot.tf` · **Size:** S

`aws_iot_policy.rover` grants Connect/Publish/Subscribe on `Resource = "*"`.
Any device certificate can connect under any client ID and read every rover's
topics, which throws away the main reason for per-device certificates: a stolen
rover becomes a fleet-wide listener.

```hcl
Resource = "arn:aws:iot:${region}:${account}:client/$${iot:Connection.Thing.ThingName}"
Resource = "arn:aws:iot:${region}:${account}:topic/rover/$${iot:Connection.Thing.ThingName}/telemetry"
```

Cheap with one rover, painful to retrofit across a fleet.

---

## Infrastructure

### Terraform state in S3

**Where:** `mt0d12-infrastructure/terraform/terraform.tf` · **Size:** S

State is a local file on one machine. Only that machine can run terraform, and
losing the file means terraform forgets it owns the VPC, RDS and IoT thing -
recovery is `terraform import` on every resource by hand. It also holds the
database password in clear text.

An S3 backend with `encrypt = true`, bucket versioning and `use_lockfile = true`
fixes all three. The bucket must exist first, then `terraform init
-migrate-state`.

### A way to query the database

**Where:** `mt0d12-infrastructure/terraform/` · **Size:** M

`rover-db` is private and RDS has no table browser in the console, so there is
no way to run SQL against it today. The Lambda's `{"stats": true}` mode covers
"is data arriving"; it does not cover anything else.

An SSM bastion (t4g.nano, IAM role, no inbound ports) plus port forwarding gives
`psql`/DBeaver access, and is also what migrations would run through.

### Database password in Secrets Manager

**Where:** `mt0d12-infrastructure/terraform/lambda.tf` · **Size:** S

`var.db_password` is passed to the Lambda as a plain environment variable,
visible to anyone with console access to the function. Secrets Manager with
rotation is the upgrade.

### Harden the RDS instance

**Where:** `mt0d12-infrastructure/terraform/database.tf` · **Size:** S

`skip_final_snapshot = true`, no `storage_encrypted`, no backup retention, no
deletion protection. Fine for a prototype; revisit before anything is stored
that would hurt to lose.

---

## Housekeeping

- **`start_api.sh` venv mismatch** - sources `.venv-3.12`, the README uses
  `venv`. Whichever is right, they should agree.
- **Telemetry only publishes from the API process.** `start_vehicle.sh` runs
  `apps.vehicle_control.main`, which has no publisher, so driving without the
  API sends nothing. Either document it or move the publisher.
- **Lambda logs nothing on success,** so "it worked" is inferred from the
  absence of a traceback.
- **uvicorn hangs on shutdown while a WebSocket viewer is connected.** On
  Python 3.12+ (the Pi's venv), uvicorn 0.22 awaits `server.wait_closed()`,
  which now waits for open connections, before it closes them - so SIGTERM
  never finishes while the status page is open, and only SIGKILL stops it.
  uvicorn 0.54 orders these correctly and ships a sans-I/O implementation on
  the current websockets API, so the `websockets==13.1` pin and its comment can
  go too. Both are pure-Python wheels that install on armv6. Check on the Pi:
  the install, startup time and memory on 512 MB, and a clean SIGTERM with the
  page open.

## If the fleet grows past one rover

Not needed now; the numbers change at scale.

- **Batch telemetry** into one message per window rather than per sample - IoT
  Core bills per message.
- **RDS Proxy** in front of Postgres: one Lambda invocation per message means
  Lambda concurrency, not Postgres, hits the connection limit first.
- **Archive raw telemetry to S3** via a second IoT rule and keep only recent
  data in Postgres - roughly 5x cheaper per GB.
- **Reconsider Postgres** for high-rate sensor streams: state and events belong
  in Postgres, continuous 100 Hz data does not.
