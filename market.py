"""Market-hours helpers (US Eastern time)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def et_hm() -> str:
    """Current ET time as 'HH:MM'."""
    return now_et().strftime("%H:%M")


def is_weekday(dt: datetime | None = None) -> bool:
    return (dt or now_et()).weekday() < 5


def time_in_window(start_hm: str, end_hm: str, now_hm: str | None = None) -> bool:
    n = now_hm or et_hm()
    return start_hm <= n <= end_hm


def in_orb_window() -> bool:
    """True while inside the opening-range window (inclusive)."""
    return time_in_window("09:30", "10:00")


def bar_et_hm(ts: str) -> str:
    """ET 'HH:MM' for an Alpaca bar/quote timestamp (ISO8601, e.g. ...Z)."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
    return dt.strftime("%H:%M")
