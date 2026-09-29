"""What the flight controller actually says on the rover's MAVLink port.

Run on the Pi from the project root, with the API stopped:

    python fc_survey.py                 # FC_DEVICE / FC_BAUDRATE from settings
    python fc_survey.py --seconds 30    # a longer listen, for slow messages
    python fc_survey.py --listen-only   # send nothing to the FC at all

The API holds the same UART open; two readers on one serial port split the
bytes between them, so both see broken frames. Stop the API first.

Three parts, in this order so the requests cannot skew the counts:

1. Listen: every message type, how often it arrives and from which
   system/component, plus the FC's status texts and sensor health.
2. Ask the FC what firmware it runs (`AUTOPILOT_VERSION`).
3. Read the serial-port and stream-rate parameters (`SERIALn_*`, `SRn_*`),
   which decide what this port sends and how fast.

Parts 2 and 3 only read: a request for one message and `PARAM_REQUEST_READ`.
Nothing is written to the FC's parameters and nothing moves.
"""

import argparse
import sys
import time
from collections import Counter
from typing import Any

from pymavlink import mavutil

from lib.common.formatting import print_error, print_info, print_success, print_warning
from settings import FC_BAUDRATE, FC_DEVICE

mavlink = mavutil.mavlink

HEARTBEAT_TIMEOUT_SECONDS = 5.0
RECV_TIMEOUT_SECONDS = 0.5
QUERY_TIMEOUT_SECONDS = 3.0

# What the backlog's FC items would read, and the rate each wants. Anything
# listed that never arrives is reported, since that is the finding that
# changes plans.
WANTED = {
    "ATTITUDE": "tilt stop, heading hold - wants 20 Hz or more",
    "SYS_STATUS": "battery and sensor health (already used)",
    "STATUSTEXT": "FC messages on the status page",
    "SCALED_IMU": "bump detection",
    "RAW_IMU": "bump detection",
    "VIBRATION": "vibration / rough ground",
    "BATTERY_STATUS": "per-cell battery",
    "RC_CHANNELS": "RC kill switch",
    "GPS_RAW_INT": "position",
    "GLOBAL_POSITION_INT": "position",
}

# SERIALn is the FC's UART n; SRn_* are the stream rates (Hz) it sends on it.
# EXTRA1 carries ATTITUDE, EXT_STAT carries SYS_STATUS, RAW_SENS the IMU.
SERIAL_PORTS = range(8)
STREAM_PORTS = range(7)
STREAM_GROUPS = ["EXTRA1", "EXT_STAT", "RAW_SENS", "EXTRA3", "RC_CHAN", "POSITION"]
MAVLINK_PROTOCOLS = {1: "MAVLink1", 2: "MAVLink2"}


def enum_name(enum: str, value: int, prefix: str = "") -> str:
    entry = mavlink.enums.get(enum, {}).get(value)
    if entry is None:
        return str(value)
    return entry.name.removeprefix(prefix or f"{enum}_")


def describe_heartbeat(heartbeat: Any) -> None:
    armed = bool(heartbeat.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
    print_info("Heartbeat")
    print(f"  system/component: {heartbeat.get_srcSystem()}/{heartbeat.get_srcComponent()}")
    print(f"  vehicle type:     {enum_name('MAV_TYPE', heartbeat.type)}")
    print(f"  autopilot:        {enum_name('MAV_AUTOPILOT', heartbeat.autopilot)}")
    print(f"  mode:             {mavutil.mode_string_v10(heartbeat)}")
    print(f"  armed:            {armed}")
    print(f"  system status:    {enum_name('MAV_STATE', heartbeat.system_status)}")


def listen(link: Any, seconds: float) -> dict[str, Any]:
    """Count everything that arrives for `seconds`."""
    counts: Counter[str] = Counter()
    sources: Counter[tuple[int, int]] = Counter()
    last: dict[str, Any] = {}
    texts: list[tuple[str, str]] = []
    started = time.monotonic()
    deadline = started + seconds
    while (now := time.monotonic()) < deadline:
        message = link.recv_match(blocking=True, timeout=min(RECV_TIMEOUT_SECONDS, deadline - now))
        if message is None:
            continue
        kind = message.get_type()
        counts[kind] += 1
        if kind == "BAD_DATA":
            continue
        sources[(message.get_srcSystem(), message.get_srcComponent())] += 1
        last[kind] = message
        if kind == "STATUSTEXT":
            texts.append((enum_name("MAV_SEVERITY", message.severity), message.text))
    return {
        "elapsed": time.monotonic() - started,
        "counts": counts,
        "sources": sources,
        "last": last,
        "texts": texts,
    }


def report_rates(survey: dict[str, Any]) -> None:
    elapsed, counts = survey["elapsed"], survey["counts"]
    print_info(f"Messages over {elapsed:.1f}s")
    for kind, count in sorted(counts.items(), key=lambda item: -item[1]):
        if kind == "BAD_DATA":
            continue
        print(f"  {kind:<28} {count:>6}  {count / elapsed:6.1f} Hz")

    bad = counts.get("BAD_DATA", 0)
    good = sum(counts.values()) - bad
    if bad:
        # Garbage on a UART is a wrong baud rate, a second reader on the port
        # or a noisy wire, in roughly that order of likelihood.
        print_warning(
            f"  {bad} unparseable chunks against {good} good messages - "
            "check the baud rate, and that nothing else has the port open"
        )

    print_info("Senders (system/component)")
    for (system, component), count in survey["sources"].most_common():
        print(f"  {system}/{component:<6} {count:>6} messages")


def report_wanted(survey: dict[str, Any]) -> None:
    elapsed, counts = survey["elapsed"], survey["counts"]
    print_info("What the backlog items would need")
    for kind, purpose in WANTED.items():
        count = counts.get(kind, 0)
        line = f"  {kind:<20} {purpose}"
        if count:
            print_success(f"{line}: {count / elapsed:.1f} Hz")
        else:
            print_warning(f"{line}: not sent")


def report_health(sys_status: Any | None) -> None:
    print_info("Sensor health (SYS_STATUS)")
    if sys_status is None:
        print_warning("  No SYS_STATUS arrived")
        return
    for bit, entry in mavlink.enums["MAV_SYS_STATUS_SENSOR"].items():
        if entry.name.endswith("ENUM_END") or not sys_status.onboard_control_sensors_present & bit:
            continue
        name = entry.name.removeprefix("MAV_SYS_STATUS_")
        enabled = bool(sys_status.onboard_control_sensors_enabled & bit)
        healthy = bool(sys_status.onboard_control_sensors_health & bit)
        line = f"  {name:<32} {'enabled' if enabled else 'disabled':<9}"
        if healthy:
            print_success(f"{line} healthy")
        elif enabled:
            print_error(f"{line} UNHEALTHY")
        else:
            print(f"{line} -")


def report_texts(texts: list[tuple[str, str]]) -> None:
    print_info("Status texts")
    if not texts:
        # ArduPilot says most of its piece at boot; a running FC is quiet.
        print("  None during the listen - reboot the FC mid-listen to catch the boot messages")
    for severity, text in texts:
        print(f"  [{severity}] {text}")


def query_version(link: Any) -> None:
    print_info("Firmware (AUTOPILOT_VERSION)")
    link.mav.command_long_send(
        link.target_system,
        link.target_component,
        mavlink.MAV_CMD_REQUEST_MESSAGE,
        0,
        mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    message = link.recv_match(
        type="AUTOPILOT_VERSION", blocking=True, timeout=QUERY_TIMEOUT_SECONDS
    )
    if message is None:
        print_warning(f"  No reply within {QUERY_TIMEOUT_SECONDS:g}s")
        return
    version = message.flight_sw_version
    release = enum_name("FIRMWARE_VERSION_TYPE", version & 0xFF)
    git_hash = bytes(message.flight_custom_version).hex()
    print(
        f"  version: {version >> 24 & 0xFF}.{version >> 16 & 0xFF}.{version >> 8 & 0xFF} ({release})"
    )
    print(f"  git:     {git_hash}")
    print(f"  board:   {message.board_version:#x}")


def query_params(link: Any, names: list[str]) -> dict[str, float]:
    for name in names:
        link.param_fetch_one(name)
    wanted = set(names)
    values: dict[str, float] = {}
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    while wanted - values.keys() and (now := time.monotonic()) < deadline:
        message = link.recv_match(
            type="PARAM_VALUE", blocking=True, timeout=min(RECV_TIMEOUT_SECONDS, deadline - now)
        )
        if message is not None and message.param_id in wanted:
            values[message.param_id] = message.param_value
    return values


def report_ports(link: Any, device: str, baudrate: int) -> None:
    serial_names = [f"SERIAL{n}_{field}" for n in SERIAL_PORTS for field in ("PROTOCOL", "BAUD")]
    stream_names = [f"SR{n}_{group}" for n in STREAM_PORTS for group in STREAM_GROUPS]
    values = query_params(link, serial_names + stream_names)
    if not values:
        print_warning("Serial ports: no parameter replies - not ArduPilot, or the link is one-way")
        return

    # ArduPilot stores 115200 as 115, 921600 as 921; 57 is 57600.
    baud_param = baudrate // 1000
    # SERIAL0 is the FC's USB port, never a UART such as /dev/serial0.
    over_usb = "ttyACM" in device or "usb" in device.lower()
    print_info("Serial ports (params)")
    print(
        f"  {'port':<9} {'protocol':<12} {'baud':>6}  " + " ".join(f"{g:>8}" for g in STREAM_GROUPS)
    )
    for n in SERIAL_PORTS:
        protocol = values.get(f"SERIAL{n}_PROTOCOL")
        if protocol is None:
            continue
        baud = values.get(f"SERIAL{n}_BAUD")
        rates = [values.get(f"SR{n}_{group}") for group in STREAM_GROUPS]
        protocol_name = MAVLINK_PROTOCOLS.get(int(protocol), f"other ({protocol:g})")
        line = (
            f"  SERIAL{n:<3} {protocol_name:<12} {'-' if baud is None else f'{baud:g}':>6}  "
            + " ".join(f"{'-' if rate is None else f'{rate:g}':>8}" for rate in rates)
        )
        # Which SERIALn this link is cannot be asked; a MAVLink port at our
        # baud rate, on the right side of USB/UART, is the likely one.
        likely = (
            int(protocol) in MAVLINK_PROTOCOLS
            and (n == 0) == over_usb
            and (over_usb or (baud is not None and int(baud) == baud_param))
        )
        if likely:
            print_success(f"{line}   <- likely this link")
        else:
            print(line)
    print("  SRn_* are the rates in Hz the FC sends each group at on that port.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", default=FC_DEVICE, help=f"default: {FC_DEVICE}")
    parser.add_argument("--baud", type=int, default=FC_BAUDRATE, help=f"default: {FC_BAUDRATE}")
    parser.add_argument("--seconds", type=float, default=10.0, help="listen time, default 10")
    parser.add_argument(
        "--listen-only", action="store_true", help="skip the firmware and parameter queries"
    )
    args = parser.parse_args()

    if not args.device:
        print_error("No device: set FC_DEVICE or pass --device")
        return 1

    print_warning("The API must be stopped: it reads this same port.")
    print_info(f"Opening {args.device} at {args.baud} baud")
    try:
        link = mavutil.mavlink_connection(args.device, baud=args.baud)
    except Exception as exc:
        print_error(f"Could not open the port: {exc}")
        return 1

    try:
        heartbeat = link.wait_heartbeat(timeout=HEARTBEAT_TIMEOUT_SECONDS)
        if heartbeat is None:
            print_error(
                f"No heartbeat within {HEARTBEAT_TIMEOUT_SECONDS:g}s - wrong device or baud rate, "
                "the FC is off, or this UART is not set to MAVLink"
            )
            return 1
        describe_heartbeat(heartbeat)
        print(f"  wire protocol:    MAVLink {link.WIRE_PROTOCOL_VERSION}")

        print_info(f"Listening for {args.seconds:g}s...")
        survey = listen(link, args.seconds)
        report_rates(survey)
        report_wanted(survey)
        report_health(survey["last"].get("SYS_STATUS"))
        report_texts(survey["texts"])

        if not args.listen_only:
            query_version(link)
            report_ports(link, args.device, args.baud)
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
