"""Shared time parsing and formatting helpers."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone


def now_ts() -> int:
    """Return the current Unix timestamp in seconds."""
    return int(time.time())


def timestamp_to_iso(ts: int | float | None) -> str:
    """Return a stable UTC ISO-8601 string for a Unix timestamp."""
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def timestamp_to_display(
    ts: int | float | None,
    *,
    tz: timezone | None = None,
) -> str:
    """Return a compact display string in the system local timezone.

    Human-facing output uses local time with an explicit UTC-offset
    label (a UK user should see 18:00 during summer time, not 17:00).
    Machine-readable output keeps UTC ISO-8601 via
    :func:`timestamp_to_iso`. Pass ``tz`` to override the zone (tests
    pass ``timezone.utc`` for determinism).
    """
    if not ts:
        return ""
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    dt = dt.astimezone(tz) if tz is not None else dt.astimezone()
    offset = dt.utcoffset()
    if not offset:
        label = "UTC"
    else:
        total = int(offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total) // 60, 60)
        label = f"UTC{sign}{hours:02d}:{minutes:02d}"
    return f"{dt.strftime('%b %d %H:%M')} {label}"


def calculate_days_left(ts: int | float | None, *, now: int | float | None = None) -> int | None:
    """Return whole days from *now* until *ts*, or None when no timestamp exists."""
    if not ts:
        return None
    base = time.time() if now is None else now
    return int((int(ts) - base) / 86400)


def parse_interval(value: str | None, *, default: int = 900) -> int:
    """Parse an interval like ``900``, ``15m``, ``2h``, or ``1d`` into seconds.

    Bare numbers are seconds. Raises ``ValueError`` for anything else.
    """
    if value is None:
        return default
    raw = value.strip().lower()
    if not raw:
        return default
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", raw)
    if not match:
        raise ValueError("interval must be like 900, 30s, 15m, 2h, or 1d")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_since(value: str | None, *, now: int | float | None = None) -> int | None:
    """Parse ``7d``, ``24h``, or ``YYYY-MM-DD`` into a Unix timestamp."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    base = int(time.time() if now is None else now)
    rel = re.fullmatch(r"(\d+)\s*([dh])", raw, flags=re.IGNORECASE)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2).lower()
        seconds = amount * (86400 if unit == "d" else 3600)
        return base - seconds

    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(
            "--since must be like 7d, 24h, or YYYY-MM-DD"
        ) from exc
    return int(dt.timestamp())
