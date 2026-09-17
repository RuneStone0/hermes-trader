"""Tests for the mean-reversion sleeve, including a live-vs-backtest cross-check.

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_mr.py

The most important test here is `test_live_rule_matches_backtest`: the sleeve's
edge was measured by `backtest_mr.build_mr_rsi2`, so if the live rule's signal
ever drifts from that function the bot is trading a rule nobody measured — the
classic way a backtest looks good while the live bot loses. Both are run over the
same synthetic series and must agree on signalling AND on the stop level.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest_mr                                       # noqa: E402
import config                                            # noqa: E402
import indicators                                        # noqa: E402
import mr_run                                            # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def _series(closes: list[float]) -> list[dict]:
    return [{"t": f"2026-{(i // 21) + 1:02d}-{(i % 21) + 1:02d}",
             "o": c, "h": c * 1.004, "l": c * 0.996, "c": c, "v": 1_000_000}
            for i, c in enumerate(closes)]


def _uptrend_with_dip(dip: float = 0.97, n: int = 260) -> list[dict]:
    """A steady uptrend, then two sharp down days at the end (an RSI(2) dip in
    an uptrend — exactly what the sleeve is supposed to buy)."""
    base = [100.0 * (1.0012 ** i) for i in range(n - 2)]
    closes = base + [base[-1] * dip, base[-1] * dip * 0.995]
    return _series(closes)


def test_rsi_mirrors_the_backtester() -> None:
    print("RSI(2) implementation matches the backtester's")
    closes = [10, 11, 12, 11.5, 11, 12.5, 13, 12.8, 12.1, 11.4, 12.9, 13.4]
    live = indicators.rsi(closes, 2)
    ref = backtest_mr.rsi(closes, 2)
    check("same length", len(live) == len(ref))
    same = all((a is None and b is None) or (a is not None and b is not None
                                             and abs(a - b) < 1e-12)
               for a, b in zip(live, ref))
    check("identical values (the live rule IS the measured rule)", same,
          f"live[-1]={live[-1]} ref[-1]={ref[-1]}")


def test_live_rule_matches_backtest() -> None:
    print("live signal() agrees with the backtested build_mr_rsi2 on the same bars")
    cases = {
        "uptrend with a 2-day dip": _uptrend_with_dip(),
        "monotone uptrend (no dip)": _series([100.0 * (1.0012 ** i) for i in range(260)]),
        "downtrend": _series([200.0 * (0.999 ** i) for i in range(260)]),
        "flat chop": _series([100.0 + (i % 3) for i in range(260)]),
    }
    for label, bars in cases.items():
        sigs, _exits = backtest_mr.build_mr_rsi2(bars)
        ref_fired = sigs.get(len(bars) - 1)
        live = mr_run.signal(bars, live=None)
        if ref_fired is None:
            check(f"{label}: both stand down", live is None, str(live)[:60])
        else:
            check(f"{label}: both signal", live is not None)
            if live:
                check(f"{label}: same stop level",
                      abs(live["stop"] - ref_fired["stop"]) < 1e-9,
                      f"live {live['stop']:.4f} vs backtest {ref_fired['stop']:.4f}")


def test_entry_geometry() -> None:
    print("entry geometry (stop 2.5x ATR, wide outer target)")
    bars = _uptrend_with_dip()
    live = mr_run.signal(bars, live=None)
    check("a dip in an uptrend signals", live is not None)
    if not live:
        return
    mr = config.MR
    check("stop is stop_atr x ATR below the entry",
          abs((live["entry"] - live["stop"]) - float(mr["stop_atr"]) * live["atr"]) < 1e-9)
    check("target is the wide outer cap",
          abs((live["target"] - live["entry"]) - float(mr["target_atr"]) * live["atr"]) < 1e-9)
    check("stop is below entry and above zero", 0 < live["stop"] < live["entry"])
    check("RSI(2) is below the entry threshold", live["rsi2"] < float(mr["rsi_entry"]),
          f"{live['rsi2']:.1f}")


def test_no_trade_without_enough_history() -> None:
    print("insufficient history => no signal (never a crash)")
    short = _series([100.0 + i * 0.1 for i in range(60)])
    check("60 bars is not enough for a 200d filter", mr_run.signal(short, live=None) is None)


def test_exit_rule() -> None:
    print("exit rule: close back above SMA(5)")
    up = _series([100.0, 100.5, 101.0, 101.5, 102.0, 103.5])
    hit, why = mr_run.exit_rule_hit(up, live=None)
    check("a close above the 5-day average exits", hit is True, why)
    down = _series([103.0, 102.0, 101.0, 100.0, 99.0, 97.5])
    hit2, why2 = mr_run.exit_rule_hit(down, live=None)
    check("a close below the 5-day average holds", hit2 is False, why2)


def test_exit_rule_uses_the_live_price_as_the_forming_close() -> None:
    print("exit rule evaluates the live price as today's close")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # The last bar must carry TODAY's date, otherwise _with_live_close rightly
    # refuses to inject the live price (a stale bar is not a forming bar).
    bars = _series([100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    bars[-1]["t"] = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    hit_low, _ = mr_run.exit_rule_hit(bars, live=95.0)
    hit_high, why = mr_run.exit_rule_hit(bars, live=110.0)
    check("a live print below the average does not exit", hit_low is False)
    check("a live print above the average exits", hit_high is True, why)
    # And with no live price at all it falls back to the last close (in range).
    stale = _series([100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    hit_none, _ = mr_run.exit_rule_hit(stale, live=None)
    check("without a live print it uses the last close (no false exit)",
          hit_none is False)


def test_live_close_replaces_only_todays_bar() -> None:
    print("the forming bar's close is replaced, history is untouched")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    bars = _series([100.0, 101.0, 102.0])
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    bars[-1]["t"] = today
    out = mr_run._with_live_close(bars, 150.0)
    check("today's close becomes the live price", out[-1]["c"] == 150.0)
    check("earlier closes are unchanged",
          [b["c"] for b in out[:2]] == [b["c"] for b in bars[:2]])
    check("today's high/low expand to contain the live price",
          out[-1]["h"] >= 150.0 and out[-1]["l"] <= 150.0)
    check("the input is not mutated", bars[-1]["c"] == 102.0)


def test_config_wiring() -> None:
    print("MR config is wired into the tuner safely")
    knobs = {k["key"] for k in config.tunable_knobs() if k["section"] == "MR"}
    check("MR knobs are whitelisted", {"risk_pct", "rsi_entry", "stop_atr"} <= knobs,
          str(sorted(knobs)))
    check("the universe is NOT tunable by the loop", "universe" not in knobs)
    check("the variant is NOT tunable by the loop", "variant" not in knobs)
    check("DAILY carries its own (halved) risk budget",
          config.DAILY.get("risk_pct") == 0.005, str(config.DAILY.get("risk_pct")))
    check("MR risk is above the ORB's, matching the evidence",
          config.MR["risk_pct"] > config.DAILY.get("risk_pct", 0))
    # An override cannot smuggle in an unknown key or a floor change.
    clean, notes = config.propose_override(
        {"MR": {"risk_pct": 0.05, "universe": ["TSLA"], "enabled": False}})
    check("out-of-bounds risk is clamped to the whitelist ceiling",
          clean.get("MR", {}).get("risk_pct") == config.MR_TUNE["risk_pct"][1],
          str(clean))
    check("universe/enabled are dropped", "universe" not in clean.get("MR", {})
          and "enabled" not in clean.get("MR", {}))


def main() -> int:
    print(f"config VERSION {config.VERSION}")
    for fn in (test_rsi_mirrors_the_backtester, test_live_rule_matches_backtest,
               test_entry_geometry, test_no_trade_without_enough_history,
               test_exit_rule, test_exit_rule_uses_the_live_price_as_the_forming_close,
               test_live_close_replaces_only_todays_bar, test_config_wiring):
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("all mean-reversion sleeve tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
