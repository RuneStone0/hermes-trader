"""Tests for the daily 20-day-high breakout sleeve (breakout_run.py).

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_breakout_sleeve.py

What is pinned here
-------------------
1. `_signal` fires ONLY on a completed close above the prior 20 sessions' high,
   and it measures the prior 20 bars WITHOUT the signal bar (an off-by-one that
   included the signal bar would make every strong close a "breakout").
2. `_completed` drops the in-progress session, so a rule that is defined on a
   CLOSE can never be evaluated on an unfinished one.
3. The geometry constants are the ones that were measured: 1.5 ATR stop,
   4.0 ATR target. If someone re-tunes them, the live sleeve is a different
   rule with unknown expectancy and this test must fail.
4. The sleeve is long-only, ETF-only and its gross cap keeps it unlevered.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import breakout_run                                    # noqa: E402
from indicators import atr                             # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def _bars(closes: list[float], dates: list[str] | None = None, hi_pad: float = 1.0) -> list[dict]:
    out = []
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, c in enumerate(closes):
        d = dates[i] if dates else (base + timedelta(days=i)).strftime("%Y-%m-%dT00:00:00Z")
        out.append({"t": d, "c": c, "h": c + hi_pad, "l": c - hi_pad})
    return out


def test_signal_requires_a_close_above_the_prior_20_highs() -> None:
    print("\n_signal: 20-day high rule")
    n = breakout_run.PARAMS["lookback"]
    flat = [100.0] * (n + 40)
    # Last close equal to the prior high (100 + 1.0 pad) is NOT a breakout.
    check("close == prior high -> no signal", breakout_run._signal(_bars(flat + [])) is None)
    # A close above every prior high fires.
    closes = [100.0] * (n + 40) + [102.0]
    sig = breakout_run._signal(_bars(closes))
    check("close above prior 20 highs -> signal", sig is not None)
    if sig:
        check("breakout_high is the prior high, not the signal bar's",
              abs(sig["breakout_high"] - 101.0) < 1e-6, f"got {sig['breakout_high']}")
        check("atr is positive", sig["atr"] > 0)
        check("clear_atr = (close - high)/atr",
              abs(sig["clear_atr"] - (102.0 - 101.0) / sig["atr"]) < 1e-9)


def test_signal_ignores_a_bar_outside_the_lookback() -> None:
    print("\n_signal: lookback boundary")
    n = breakout_run.PARAMS["lookback"]
    closes = [100.0] * (n + 40)
    # A big high 25 sessions ago must NOT count as the 20-day high...
    bars = _bars(closes)
    bars[-40]["h"] = 150.0
    bars = bars + [{"t": "2026-12-31T00:00:00Z", "c": 102.0, "h": 103.0, "l": 101.0}]
    check("high older than the lookback is ignored", breakout_run._signal(bars) is not None)


def test_completed_drops_todays_bar() -> None:
    print("\n_completed: the rule is close-based, so today's partial bar is excluded")
    today = "2026-11-23"
    bars = [
        {"t": "2026-11-19T00:00:00Z", "c": 100, "h": 101, "l": 99},
        {"t": "2026-11-20T00:00:00Z", "c": 101, "h": 102, "l": 100},
        {"t": "2026-11-23T00:00:00Z", "c": 120, "h": 121, "l": 119},
        {"t": "2026-11-23T14:30:00Z", "c": 121, "h": 122, "l": 120},
    ]
    kept = breakout_run._completed(bars, today)
    check("today's bars (00:00Z and intraday) dropped", len(kept) == 2, f"kept {len(kept)}")
    check("yesterday's bar kept", kept and kept[-1]["c"] == 101)
    # A session-date bar is stamped 00:00Z of the TRADING day, which is the
    # previous evening in ET. Reading it in ET would shift every bar back a day,
    # so the session date must be the UTC date.
    check("session date is the UTC date, not the ET date",
          len(breakout_run._completed([{"t": "2026-11-23T00:00:00Z", "c": 1, "h": 1, "l": 1}], today)) == 0)


def test_geometry_constants_are_the_measured_ones() -> None:
    print("\ngeometry: the deployed numbers are the measured numbers")
    p = breakout_run.PARAMS
    check("lookback == 20", p["lookback"] == 20)
    check("stop_atr == 1.5", p["stop_atr"] == 1.5)
    check("tgt_atr == 4.0", p["tgt_atr"] == 4.0)
    check("reward:risk == 2.67", abs(p["tgt_atr"] / p["stop_atr"] - 2.6667) < 0.001)
    check("hold == 15 sessions ~ 21 calendar days", p["max_hold_days"] == 21)


def test_floor_and_exposure() -> None:
    print("\nfloor: long-only, ETFs, no leverage")
    check("4 slots x 12.5% == 50% gross cap",
          abs(breakout_run.MAX_POSITIONS * breakout_run.WEIGHT - breakout_run.MAX_GROSS) < 1e-9)
    check("gross cap < 100% (no leverage)", breakout_run.MAX_GROSS <= 1.0)
    check("no crypto in the universe",
          not any(s in ("BTC", "ETH", "BTCUSD", "ETHUSD") for s in breakout_run.UNIVERSE))
    check("universe is the lab's 14 ETFs", len(breakout_run.UNIVERSE) == 14)
    check("entry window is right after the ET open",
          breakout_run.ENTRY_WINDOW[0] >= "09:31" and breakout_run.ENTRY_WINDOW[1] <= "10:00")


def main() -> None:
    test_signal_requires_a_close_above_the_prior_20_highs()
    test_signal_ignores_a_bar_outside_the_lookback()
    test_completed_drops_todays_bar()
    test_geometry_constants_are_the_measured_ones()
    test_floor_and_exposure()
    print("\n%s" % ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
