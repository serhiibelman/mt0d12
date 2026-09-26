from collections import namedtuple

import pytest

from apps.api.services.pi_health import (
    PI_HEALTH_UNAVAILABLE,
    PiHealthReader,
    parse_throttled,
)
from apps.api.services.vehicle_status import VehicleStatusService

MEMINFO = """MemTotal:         444764 kB
MemFree:           61236 kB
MemAvailable:     215040 kB
Buffers:           20480 kB
"""

DiskUsage = namedtuple("DiskUsage", "total used free")


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """The files a Pi exposes, written into tmp_path."""
    thermal = tmp_path / "temp"
    thermal.write_text("51540\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO)
    throttled = tmp_path / "get_throttled"
    throttled.write_text("0\n")
    monkeypatch.setattr(
        "apps.api.services.pi_health.shutil.disk_usage",
        lambda _path: DiskUsage(total=16_000 * 2**20, used=6_000 * 2**20, free=10_000 * 2**20),
    )
    return {"thermal_path": thermal, "meminfo_path": meminfo, "throttled_path": throttled}


def make_reader(board, **overrides):
    fields = {**board, "loadavg": lambda: (0.42, 0.3, 0.2), "run_vcgencmd": _no_vcgencmd}
    fields.update(overrides)
    return PiHealthReader(**fields)


def _no_vcgencmd() -> str:
    raise FileNotFoundError("vcgencmd")


def test_gauges_come_out_in_the_units_their_names_say(board) -> None:
    health = make_reader(board)()

    assert health["cpu_temp_c"] == 51.5
    assert health["load_1m"] == 0.42
    assert health["memory_available_mb"] == 210
    assert health["memory_available_percent"] == 48
    assert health["disk_free_mb"] == 10_000
    assert health["disk_free_percent"] == 62
    assert health["warnings"] == []


@pytest.mark.parametrize(
    "text, word",
    [("throttled=0x50005\n", 0x50005), ("50005\n", 0x50005), ("throttled=0x0", 0)],
)
def test_the_throttle_word_parses_from_vcgencmd_and_sysfs(text, word) -> None:
    assert parse_throttled(text) == word


def test_a_brownout_that_has_passed_still_shows_since_boot(board) -> None:
    # 0x50000: undervoltage and throttling happened, neither is happening now.
    # This is the case that otherwise goes unseen - Wi-Fi died at boot, the
    # voltage recovered, and nothing on the running Pi says why.
    board["throttled_path"].write_text("50000\n")

    health = make_reader(board)()

    assert health["throttled_raw"] == "0x50000"
    assert health["undervoltage_now"] is False
    assert health["undervoltage_since_boot"] is True
    assert health["throttled_since_boot"] is True
    assert health["freq_capped_since_boot"] is False
    assert health["warnings"] == []


def test_undervoltage_right_now_is_a_warning(board) -> None:
    board["throttled_path"].write_text("50005\n")

    health = make_reader(board)()

    assert health["undervoltage_now"] is True
    assert health["throttled_now"] is True
    assert health["warnings"] == ["undervoltage", "throttled"]


def test_vcgencmd_is_the_fallback_when_sysfs_has_no_throttle_file(board, tmp_path) -> None:
    board["throttled_path"] = tmp_path / "missing"

    health = make_reader(board, run_vcgencmd=lambda: "throttled=0x1\n")()

    assert health["undervoltage_now"] is True


def test_a_missing_vcgencmd_is_not_spawned_again(board, tmp_path) -> None:
    # Off a Pi it fails every probe, and a failed spawn still costs a fork.
    board["throttled_path"] = tmp_path / "missing"
    calls = []

    def vcgencmd() -> str:
        calls.append(1)
        raise FileNotFoundError("vcgencmd")

    reader = make_reader(board, run_vcgencmd=vcgencmd)
    reader()
    health = reader()

    assert len(calls) == 1
    assert health["throttled_raw"] is None
    assert health["undervoltage_now"] is None


def test_thresholds_raise_warnings(board, monkeypatch) -> None:
    board["thermal_path"].write_text("81200\n")
    board["meminfo_path"].write_text("MemTotal: 444764 kB\nMemAvailable: 30000 kB\n")
    monkeypatch.setattr(
        "apps.api.services.pi_health.shutil.disk_usage",
        lambda _path: DiskUsage(total=16_000 * 2**20, used=15_000 * 2**20, free=1_000 * 2**20),
    )

    health = make_reader(board)()

    assert health["warnings"] == ["cpu_hot", "disk_low", "memory_low"]


def test_off_a_pi_the_missing_readings_are_null_not_errors(tmp_path) -> None:
    reader = PiHealthReader(
        thermal_path=tmp_path / "none",
        meminfo_path=tmp_path / "none",
        throttled_path=tmp_path / "none",
        disk_path=str(tmp_path / "none"),
        run_vcgencmd=_no_vcgencmd,
    )

    health = reader()

    assert health["cpu_temp_c"] is None
    assert health["memory_available_mb"] is None
    assert health["disk_free_mb"] is None
    assert health["throttled_raw"] is None
    assert health["warnings"] == []


# -- in the status snapshot --------------------------------------------------


def test_the_probe_puts_pi_health_in_the_snapshot() -> None:
    reading = {**PI_HEALTH_UNAVAILABLE, "cpu_temp_c": 55.0, "warnings": []}
    service = VehicleStatusService(
        motor_device=None, fc_device=None, pi_health_reader=lambda: reading
    )

    service._probe_once()

    assert service.snapshot()["pi"]["cpu_temp_c"] == 55.0


def test_a_failing_reader_does_not_cost_the_rest_of_the_probe() -> None:
    def broken() -> dict:
        raise RuntimeError("boom")

    service = VehicleStatusService(motor_device=None, fc_device=None, pi_health_reader=broken)

    service._probe_once()
    snapshot = service.snapshot()

    assert snapshot["pi"] == PI_HEALTH_UNAVAILABLE
    assert snapshot["components"]["motor_bus"]["detail"] == "DEVICE is not configured"
