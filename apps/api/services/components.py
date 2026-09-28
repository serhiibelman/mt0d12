"""The health record every hardware component reports in the snapshot."""

from dataclasses import dataclass
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ComponentSnapshot:
    configured: bool
    connected: bool
    detail: str
    checked_at: datetime
