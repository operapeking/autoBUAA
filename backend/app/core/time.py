from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
UTC_TZ = timezone.utc


class Clock:
    """Controllable clock for deterministic scheduling and time-window testing."""

    def __init__(self, fixed_utc: datetime | None = None) -> None:
        self._fixed_utc = ensure_utc(fixed_utc) if fixed_utc is not None else None

    def now_utc(self) -> datetime:
        if self._fixed_utc is not None:
            return self._fixed_utc
        return datetime.now(UTC_TZ)

    def now_shanghai(self) -> datetime:
        return self.now_utc().astimezone(SHANGHAI_TZ)

    def set(self, dt: datetime) -> None:
        self._fixed_utc = ensure_utc(dt)

    def advance(self, seconds: float) -> datetime:
        from datetime import timedelta

        current = self.now_utc()
        updated = current + timedelta(seconds=seconds)
        self._fixed_utc = updated
        return updated


default_clock = Clock()


def ensure_utc(dt: datetime) -> datetime:
    """Convert naive (assumed UTC) or aware datetime to UTC aware datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ)


def to_shanghai(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(SHANGHAI_TZ)


def format_shanghai_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return to_shanghai(dt).isoformat()


def parse_shanghai_datetime(value: str) -> datetime:
    """Parse ISO or 'YYYY-MM-DD HH:MM[:SS]' in Asia/Shanghai and return UTC."""
    text = value.strip()
    if not text:
        raise ValueError("Empty datetime string")
    normalized = text.replace(" ", "T")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(UTC_TZ)
