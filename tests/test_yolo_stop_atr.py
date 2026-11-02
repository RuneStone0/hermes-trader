"""Pins the ATR stop floor added 2026-11-02 (VERSION 1.6.7).

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_yolo_stop_atr.py

Why this exists: the "YOLO uses noise-tight stops" defect was recorded as FIXED
in settled finding #1, but the only code-level guard was a % of price
(min_stop_dist_pct = 0.2%), which on a 1.3%-ATR ETF is a ~0.15-ATR stop. Two
post-fix entries (NVDA 0.86 ATR, AMZN 0.98 ATR) prove the prompt-level fix leaks.
The floor is now deterministic, and it must stay (a) on, (b) ATR-denominated, and
(c) outside the auto-tuner's reach.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import indicators                                      # noqa: E402
import yolo_run                                        # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def _daily(n: int = 20, hi: float = 101.0, lo: float = 99.0, close: float = 100.0) -> list[dict]:
    """Flat daily bars: TR = 2.0 exactly once the previous close is 100.0."""
    return [{"t": f"2026-10-{i + 1:02d}T04:00:00Z", "o": close, "h": hi, "l": lo,
             "c": close, "v": 1000} for i in range(n)]


class FakeClient:
    def __init__(self, bars):
        self._bars = bars

    def bars(self, sym, timeframe="1Day", limit=60, start=None, end=None):
        return {"bars": self._bars}


def test_ratio_matches_telemetry_definition():
    """ratio = risk_per_share / ATR(14).  ATR of the flat fixture is 2.0."""
    bars = _daily()
    atr = indicators.atr(bars, 14)[-1]
    check("fixture ATR(14) == 2.0", atr is not None and abs(atr - 2.0) < 1e-9, f"atr={atr}")
    c = FakeClient(bars)
    r = yolo_run._stop_atr_ratio(c, "XLF", 2.0)
    check("2.00 risk on a 2.0-ATR name == 1.00 ATR", r is not None and abs(r - 1.0) < 1e-6, f"{r}")
    r2 = yolo_run._stop_atr_ratio(c, "XLF", 0.6)
    check("0.60 risk on a 2.0-ATR name == 0.30 ATR", r2 is not None and abs(r2 - 0.3) < 1e-6, f"{r2}")


def test_short_history_is_unknown_not_zero():
    """Too few bars must return None (guard rejects), never a fabricated ratio."""
    c = FakeClient(_daily(n=10))
    check("10 bars -> None", yolo_run._stop_atr_ratio(c, "XLF", 1.0) is None)


def test_broken_feed_is_unknown():
    class Boom:
        def bars(self, *a, **k):
            raise RuntimeError("upstream down")
    try:
        got = yolo_run._stop_atr_ratio(Boom(), "XLF", 1.0)
    except Exception:
        got = "raised"
    check("feed error -> None or AlpacaError only", got is None, f"{got}")


def test_zero_risk_is_unknown():
    check("zero risk distance -> None", yolo_run._stop_atr_ratio(FakeClient(_daily()), "XLF", 0.0) is None)


def test_config_knob():
    check("YOLO.min_stop_atr present and >= 1.0", float(config.YOLO.get("min_stop_atr", 0)) >= 1.0,
          str(config.YOLO.get("min_stop_atr")))
    tunable = {k["key"] for k in config.tunable_knobs() if k["section"] == "YOLO"}
    check("min_stop_atr is NOT auto-tunable", "min_stop_atr" not in tunable, str(sorted(tunable)))
    check("min_stop_atr is NOT in YOLO_TUNE", "min_stop_atr" not in config.YOLO_TUNE)


if __name__ == "__main__":
    for fn in (test_ratio_matches_telemetry_definition, test_short_history_is_unknown_not_zero,
               test_broken_feed_is_unknown, test_zero_risk_is_unknown, test_config_knob):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    sys.exit(1 if FAILS else 0)
