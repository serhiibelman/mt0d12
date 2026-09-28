## 0. Project layout

```
apps/                  processes - each one is started by a start_*.sh
├── api/               FastAPI service on the vehicle
├── vehicle_control/   motor loop on the vehicle
└── controller/        gamepad reader on the laptop
lib/                   libraries - imported, never started
├── ddsm115/           motor driver
├── gamepad/           UDP control protocol shared by controller and vehicle
├── telemetry/         MQTT publisher for AWS IoT Core
├── spool/             on-disk outbox that survives an uplink outage
└── common/            formatting and conversion helpers
settings/              environment configuration
tests/
```

The rule is one-directional: `apps/*` may import `lib/*`, and `lib/*` never
imports `apps/*`. A package under `apps/` owns a process; anything two
processes need lives in `lib/`.

## 1. Install system dependencies

```
sudo apt update
sudo apt install libopenblas-dev liblapack-dev gfortran -y
```

## 2. Create venv

```
python3.11 -m venv ~/mt0d12/venv
source ~/mt0d12/venv/bin/activate
```

## 3. Install requirements

```
pip install -r ~/mt0d12/requirements.txt
```

## 4. Start vehicle API

Run on the vehicle:

```
uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
```

Open `http://<vehicle-host>:8000/` in a browser for the status page: battery, attitude,
components and Pi health, live over `/ws/status`. It needs nothing but the Pi, so it works
on a network without internet.

The page drives the rover too. Press **Arm**, then drag the on-screen stick (up is forward,
left/right steers) or hold W A S D / the arrow keys; **Stop** or Space stops at once and
disarms. Throttle ramps and steering mixes exactly as with the gamepad. While armed the page
sends a command every 50 ms; half a second without one - Wi-Fi drop, sleeping laptop,
hidden tab - stops the motors, and the page must be armed again. One driver at a time:
a second tab is refused, and `/motors/start` answers 503 while someone drives.
Why it is built this way: [docs/drive-over-websocket.md](docs/drive-over-websocket.md).

Available endpoints:

1. `GET /health` - API and hardware probe health.
   `GET /` serves the status page.
2. `GET /status` - current vehicle snapshot, including configured motor IDs, hardware probe status, battery, attitude and Raspberry Pi health.
   `WS /ws/status` streams the same snapshot, pushed 5 times a second, for a live view without polling.
   It also takes drive commands back: `{"type": "arm"}`, then
   `{"type": "drive", "throttle": -1..1, "steer": -1..1}` at 20 Hz, and `{"type": "stop"}`.
   Replies to those are `{"type": "drive", "armed": bool, "detail": str}`; status messages have no `type`.
3. `POST /motors/start` - ramp all motors to a requested base RPM.
4. `POST /motors/stop` - ramp all motors down to zero.
5. `GET /camera/stream` - live MJPEG video from the RPi Camera (B).
6. `GET /camera/snapshot` - single JPEG frame.
7. `GET /camera/status` - camera state, resolution, framerate and viewer count.
8. `POST /camera/start` / `POST /camera/stop` - hold the camera open or release the sensor.

Example:

```bash
curl -X POST http://<vehicle-host>:8000/motors/start \
  -H 'Content-Type: application/json' \
  -d '{"rpm": 120}'

curl -X POST http://<vehicle-host>:8000/motors/stop
```

Notes:

1. The API is intended to run on the vehicle itself so it can probe local hardware directly.
2. Motor start accepts values in the same range used by the controller logic: `-200` to `200`.
3. The motor endpoints ramp in controller-sized steps instead of jumping to the target immediately.
4. If another process already owns the motor or FC serial port, the API will report that component as unavailable and motor commands can fail with `503`.


## 5. Camera stream

The RPi Camera (B) (OV5647) is driven through `picamera2`/`libcamera`. It ships with
Raspberry Pi OS Bookworm and is **not** installed from `requirements.txt`, because it
needs the system libcamera stack and is not usable on the laptop side:

```
sudo apt install -y python3-picamera2
```

The venv on the Pi must be able to see it:

```
python3 -m venv --system-site-packages ~/mt0d12/venv
```

Check the camera is detected before starting the API: `rpicam-hello --list-cameras`.

Watch the stream in a browser, or embed it anywhere an image can go:

```html
<img src="http://<vehicle-host>:8000/camera/stream" alt="MT0D12 camera">
```

```bash
curl http://<vehicle-host>:8000/camera/snapshot -o frame.jpg
curl http://<vehicle-host>:8000/camera/status
curl -X POST http://<vehicle-host>:8000/camera/stop
```

Notes:

1. The camera opens on the first `/camera/stream` or `/camera/snapshot` request, so the
   sensor stays powered down while nobody is watching. `POST /camera/start` warms it up
   ahead of time and `POST /camera/stop` releases it.
2. All viewers share one capture pipeline and always receive the newest frame; a slow
   viewer drops frames instead of holding up capture or the motor loop.
3. `CAMERA_MAX_CLIENTS` (default 4) caps concurrent viewers; extra ones get `503`.
4. Resolution, framerate and JPEG quality come from the `CAMERA_*` variables in `.env`.
5. MJPEG is deliberately simple - every browser plays it with no JavaScript and latency
   stays low on a LAN. It costs more bandwidth than H.264; if that becomes a problem the
   next step is WebRTC, which needs a signalling server and a JS client.

### Raspberry Pi 1 Model B+

The Pi 1 is the weakest board picamera2 supports, so the defaults in `.env.example` are
sized for it (320x240 at 10 fps):

1. **32-bit Raspberry Pi OS only.** ARMv6 cannot run the 64-bit images. Use the Lite
   image - 512 MB shared with the GPU leaves no room for a desktop.
2. **The hardware JPEG encoder matters here.** `CAMERA_ENCODER=auto` tries the VideoCore
   encoder first and only falls back to software. Software JPEG has no SIMD to use on
   ARMv6 and would eat the single core the motor loop runs on. `GET /camera/status`
   reports which encoder is actually in use.
3. **Camera buffers come from CMA**, not the legacy `gpu_mem` split. If the camera fails
   to allocate buffers, raise it in `/boot/firmware/config.txt`:
   `dtoverlay=vc4-kms-v3d,cma-128`. `CAMERA_BUFFER_COUNT=2` keeps the footprint small.
4. Raise resolution and framerate gradually while watching `top` and `/camera/status`.
   USB Wi-Fi is usually the next bottleneck after the CPU.


## 6. Battery and attitude

The flight controller already knows the battery voltage and which way up the
vehicle is; both now reach `/status` and every telemetry message.

```json
"battery":  {"voltage_v": 12.4, "current_a": 1.83, "remaining_percent": 76},
"attitude": {"roll_deg": 0.4, "pitch_deg": -1.2, "yaw_deg": 271.3}
```

Notes:

1. MAVLink reports millivolts, centiamps and radians; these are volts, amps and
   degrees, with the unit in the field name. `SYS_STATUS` also reports "no
   reading" in band, as values that look plausible - 65535 mV is not a 65 V
   battery - so those become `null`.
2. Any field can be `null`: when the flight controller is not configured, not
   answering, or has no current sensor fitted.
3. Readings are taken during the existing health probe, which reopens the link
   every couple of seconds. `SYS_STATUS` streams more slowly than that, so a
   probe that catches none keeps the previous reading rather than blanking it.
   Everything is dropped as soon as the link stops answering.
4. `yaw_deg` travels in the payload but is not treated as a change, because a
   compass drifts on its own and a parked rover would otherwise publish every
   few seconds. `roll_deg` and `pitch_deg` are gravity-referenced and stay put,
   so a vehicle that tips over still reports it at once.

### Raspberry Pi health

The board's own state travels next to battery and attitude, as `pi` in
`/status` and in every telemetry message. On a Pi 1 these explain most field
failures that otherwise look like random hangs.

```json
"pi": {
  "cpu_temp_c": 51.5, "load_1m": 0.42,
  "memory_available_mb": 210, "memory_available_percent": 48,
  "disk_free_mb": 5120, "disk_free_percent": 62,
  "throttled_raw": "0x50000",
  "undervoltage_now": false, "undervoltage_since_boot": true,
  "throttled_now": false, "throttled_since_boot": true,
  "freq_capped_now": false, "freq_capped_since_boot": false,
  "soft_temp_limit_now": false, "soft_temp_limit_since_boot": false,
  "warnings": []
}
```

Notes:

1. The throttle flags are the firmware's `vcgencmd get_throttled` word, read
   from sysfs where the kernel has it and from `vcgencmd` otherwise.
   `*_since_boot` is sticky: `0x50000` above is a brownout that already passed -
   the case where Wi-Fi died at boot and nothing on the running Pi said why.
2. `warnings` lists what needs a look: `undervoltage`, `throttled`, `cpu_hot`
   (80 C and over), `disk_low` and `memory_low` (under 10% free).
3. Only the warnings and flags count as a change for telemetry. Temperature,
   load, memory and disk move on every reading, so like `yaw_deg` they travel
   in every message but never cause one.
4. Off a Pi, the readings that do not exist are `null`.

## 7. Telemetry

The API publishes vehicle snapshots to AWS IoT Core - on change, plus a
heartbeat so silence still means "gone". See `docs/MQTT.md` for the
certificates and the AWS side, and `.env.example` for every knob.

Check the uplink from the Pi before starting the API:

```
python telemetry_test.py
```

### The offline spool

Every message is written to a local SQLite file before it is sent, and deleted
once the broker acknowledges it. A publish that fails leaves the message on
disk; the next tick reconnects and drains the backlog oldest first. Losing the
link is exactly when the interesting samples happen, so they are the ones worth
keeping.

```
var/telemetry-spool.sqlite3
```

Notes:

1. `recorded_at` is stamped when the sample is taken, not when it is sent, so a
   replayed backlog lands in Postgres with the times it actually happened.
   Delivery is at-least-once, so a replay can duplicate a row rather than lose
   one.
2. Retention is capped by rows (`TELEMETRY_SPOOL_MAX_ROWS`, default 10 000) and
   by age (`TELEMETRY_SPOOL_MAX_AGE_DAYS`, default 7), both dropping the oldest
   first. An unbounded spool would fill the SD card, which takes the whole
   vehicle down - a worse failure than a gap in history.
3. `TELEMETRY_SPOOL_PATH=""` switches spooling off and restores the old
   publish-or-drop behaviour. `telemetry_test.py` always runs with it off.
4. The file is disposable. It is a buffer for messages that are already in
   Postgres or minutes from it, so deleting it costs a small gap and nothing
   else - which is also what happens automatically when the schema version
   changes between releases.

## 8. Vehicle control with gamepad

See `docs/gamepad-control.md` for the operator flow and control mapping.

Quick start:

1. On the Raspberry Pi, run `./start_vehicle.sh`.
2. On the laptop with the gamepad connected, run `./start_controller.sh`.
3. Use the controls from the gamepad doc to enable drive and move the vehicle.


# Controller mode

1. connect to vehicle via ssh ``
sudo ssh pi@192.168.0.105
``
2. on laptop side run ./start_controller.sh
3. on raspberry side run ./start_vehicle.sh
