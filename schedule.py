"""Trading schedules + next-evaluation computation.

Shared by the dashboard (to show when each bot next evaluates). Keep SCHEDULES in
sync with the JOBS list in app.py (the authoritative scheduler).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

LABELS = {"daily": "Daily ORB", "weekly": "Weekly pullback", "yolo": "YOLO"}

SCHEDULES = {
    "daily":  {"kind": "window", "start": "13:00", "end": "21:00", "interval_s": 300},
    "weekly": {"kind": "times", "times": ["14:45"]},
    "yolo":   {"kind": "times", "times": ["14:00", "17:00", "19:00"]},
}


def _parse_hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def next_run(account: str, now: datetime | None = None) -> datetime:
    """Next UTC datetime this bot will evaluate a trade decision."""
    now = now or datetime.now(timezone.utc)
    spec = SCHEDULES[account]

    if spec["kind"] == "interval":
        return now + timedelta(seconds=spec["interval_s"])

    def at(hm: str, day) -> datetime:
        h, m = _parse_hm(hm)
        return datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)

    def next_weekday_time(hm: str) -> datetime:
        for offset in range(8):
            day = now.date() + timedelta(days=offset)
            if day.weekday() < 5:
                dt = at(hm, day)
                if dt > now:
                    return dt
        return now

    if spec["kind"] == "times":
        return min(next_weekday_time(hm) for hm in spec["times"])

    # window: inside now -> next tick = now + interval; otherwise next window open
    sh, sm = _parse_hm(spec["start"])
    eh, em = _parse_hm(spec["end"])
    now_min = now.hour * 60 + now.minute
    if now.weekday() < 5 and (sh * 60 + sm) <= now_min < (eh * 60 + em):
        return now + timedelta(seconds=spec["interval_s"])
    return next_weekday_time(spec["start"])


def next_label(account: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    nr = next_run(account, now)
    delta = nr - now
    days, rem = delta.days, delta.seconds
    hours, rem = rem // 3600, rem % 3600
    mins = rem // 60
    if days:
        when = f"in {days}d {hours}h"
    elif hours:
        when = f"in {hours}h {mins}m"
    else:
        when = f"in {mins}m"
    return f"{nr.strftime('%a %H:%M')} UTC ({when})"
