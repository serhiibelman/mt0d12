"""Health of the Raspberry Pi itself: temperature, load, memory, disk, power.

On a Pi 1 these explain most field failures that otherwise look like random
hangs - thermal throttling, a full SD card, a weak supply browning the board
out. Standard library only, and every reading is optional: off a Pi (a laptop,
CI) the ones that do not exist come back as None rather than failing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

THERMAL_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
MEMINFO_PATH = Path("/proc/meminfo")
# Raspberry Pi kernels expose the firmware's throttle word here, which saves
# spawning vcgencmd every probe on a single-core board. Older kernels lack it.
THROTTLED_SYSFS_PATH = Path("/sys/devices/platform/soc/soc:firmware/get_throttled")
VCGENCMD_TIMEOUT_SECONDS = 1.0

# Bits of the firmware throttle word (`vcgencmd get_throttled`). The low bits
# are the state right now; the same bits shifted by 16 are sticky since boot,
# which is what catches a brownout that has already passed.
THROTTLE_BITS = {
    "undervoltage": 0,
    "freq_capped": 1,
    "throttled": 2,
    "soft_temp_limit": 3,
}
SINCE_BOOT_SHIFT = 16

# Thresholds that turn a number into a warning. The warnings, not the numbers,
# are what telemetry treats as news - see VOLATILE_KEYS in the publisher.
CPU_HOT_C = 80.0  # the Pi 1 firmware throttles at 85
DISK_LOW_PERCENT = 10
MEMORY_LOW_PERCENT = 10


async def _run_vcgencmd() -> str:
    """`vcgencmd get_throttled`, as a subprocess the event loop waits on.

    A firmware call that hangs is killed at the timeout rather than left
    holding a worker thread.
    """
    process = await asyncio.create_subprocess_exec(
        "vcgencmd",
        "get_throttled",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(VCGENCMD_TIMEOUT_SECONDS):
            stdout, _ = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, "vcgencmd")
    return stdout.decode()


def parse_throttled(text: str) -> int:
    """The throttle word out of `throttled=0x50005` (vcgencmd) or `50005`
    (sysfs). Both are hex; only vcgencmd says so."""
    value = text.strip().split("=")[-1]
    return int(value, 16)


def throttle_flags(word: int | None) -> dict[str, Any]:
    if word is None:
        return {
            "throttled_raw": None,
            **{f"{name}_now": None for name in THROTTLE_BITS},
            **{f"{name}_since_boot": None for name in THROTTLE_BITS},
        }
    return {
        "throttled_raw": f"0x{word:x}",
        **{f"{name}_now": bool((word >> bit) & 1) for name, bit in THROTTLE_BITS.items()},
        **{
            f"{name}_since_boot": bool((word >> (bit + SINCE_BOOT_SHIFT)) & 1)
            for name, bit in THROTTLE_BITS.items()
        },
    }


PI_HEALTH_UNAVAILABLE: dict[str, Any] = {
    "cpu_temp_c": None,
    "load_1m": None,
    "memory_available_mb": None,
    "memory_available_percent": None,
    "disk_free_mb": None,
    "disk_free_percent": None,
    **throttle_flags(None),
    "warnings": [],
}


def warnings_for(health: dict[str, Any]) -> list[str]:
    """What an operator should look at. Only known values can raise one."""
    warnings = []
    if health["undervoltage_now"]:
        warnings.append("undervoltage")
    if health["throttled_now"] or health["freq_capped_now"]:
        warnings.append("throttled")
    temp = health["cpu_temp_c"]
    if temp is not None and temp >= CPU_HOT_C:
        warnings.append("cpu_hot")
    disk = health["disk_free_percent"]
    if disk is not None and disk < DISK_LOW_PERCENT:
        warnings.append("disk_low")
    memory = health["memory_available_percent"]
    if memory is not None and memory < MEMORY_LOW_PERCENT:
        warnings.append("memory_low")
    return warnings


class PiHealthReader:
    """Reads the board's health. Awaitable-callable, so the status service can
    take any zero-argument coroutine function in its place under test."""

    def __init__(
        self,
        *,
        thermal_path: Path = THERMAL_PATH,
        meminfo_path: Path = MEMINFO_PATH,
        throttled_path: Path = THROTTLED_SYSFS_PATH,
        disk_path: str = "/",
        run_vcgencmd: Callable[[], Awaitable[str]] = _run_vcgencmd,
        loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg,
    ) -> None:
        self.thermal_path = thermal_path
        self.meminfo_path = meminfo_path
        self.throttled_path = throttled_path
        self.disk_path = disk_path
        self._run_vcgencmd = run_vcgencmd
        self._loadavg = loadavg
        # vcgencmd is only worth retrying if it was ever there: off a Pi it
        # fails every probe, and a failed spawn still costs a fork.
        self._vcgencmd_missing = False

    async def __call__(self) -> dict[str, Any]:
        health = {
            "cpu_temp_c": self._cpu_temp(),
            "load_1m": self._load(),
            **self._memory(),
            **self._disk(),
            **throttle_flags(await self._throttle_word()),
        }
        health["warnings"] = warnings_for(health)
        return health

    def _cpu_temp(self) -> float | None:
        try:
            return round(int(self.thermal_path.read_text().strip()) / 1000, 1)
        except (OSError, ValueError):
            return None

    def _load(self) -> float | None:
        try:
            return round(self._loadavg()[0], 2)
        except OSError:
            return None

    def _memory(self) -> dict[str, Any]:
        unknown = {"memory_available_mb": None, "memory_available_percent": None}
        try:
            fields = {}
            for line in self.meminfo_path.read_text().splitlines():
                name, _, rest = line.partition(":")
                fields[name] = int(rest.split()[0])  # kB
            total, available = fields["MemTotal"], fields["MemAvailable"]
        except (OSError, ValueError, KeyError, IndexError):
            return unknown
        if total <= 0:
            return unknown
        return {
            "memory_available_mb": available // 1024,
            "memory_available_percent": round(available * 100 / total),
        }

    def _disk(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.disk_path)
        except OSError:
            return {"disk_free_mb": None, "disk_free_percent": None}
        return {
            "disk_free_mb": usage.free // (1024 * 1024),
            "disk_free_percent": round(usage.free * 100 / usage.total) if usage.total else None,
        }

    async def _throttle_word(self) -> int | None:
        try:
            return parse_throttled(self.throttled_path.read_text())
        except (OSError, ValueError):
            pass
        if self._vcgencmd_missing:
            return None
        try:
            return parse_throttled(await self._run_vcgencmd())
        except FileNotFoundError:
            self._vcgencmd_missing = True
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as error:
            logger.debug("vcgencmd get_throttled failed", exc_info=error)
        return None
