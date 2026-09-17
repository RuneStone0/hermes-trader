#!/usr/bin/env python3
"""Plain-assert unit tests for backtest_mr.py - no pytest, no third-party deps.

  cd /opt/data/profiles/trader/trading && python3 tests/test_backtest_mr.py

They pin the parts of the backtester that are easy to get quietly wrong and that
the whole report rests on:
  * exact R-multiple accounting (R = net P/L / initial risk)
  * the fee model coming from fees.py and the slippage deduction
  * EXIT PRIORITY: on a single bar the stop wins over the close-based exit rule
    (OHLC bars carry no intra-bar ordering, so this is an explicit assumption -
    it is conservative, and it is asserted here so a refactor cannot silently
    flip it)
  * gap-through-stop fills at the bar open, limit targets fill with no slippage
  * sizing, max-hold, end-of-data flattening, regime labels
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import fees  # noqa: E402
import backtest_mr as bt  # noqa: E402

PASSED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name} {detail}")
    PASSED.append(name)


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def bar(d: str, o: float, h: float, l: float, c: float, hm: str = "16:00") -> dict:
    return {"d": d, "hm": hm, "o": o, "h": h, "l": l, "c": c, "v": 1000.0}


# --------------------------------------------------------------------------- #
# 1. R-multiple accounting
# --------------------------------------------------------------------------- #
def test_long_r_multiple_exact() -> None:
    """entry 100, stop 98, 10 shares, exit 104, no slippage.
    Gross = +$40, risk = $20 -> gross R = exactly +2 before fees."""
    p = bt.trade_pnl("long", 100.0, 104.0, 98.0, 10, slip_bps=0.0,
                     fee_fn=lambda *a, **k: 0.0)
    check("long gross", approx(p["gross"], 40.0), p)
    check("long risk$", approx(p["risk_dollars"], 20.0), p)
    check("long R exact", approx(p["r"], 2.0), p)

    # stop-out is exactly -1R before fees
    q = bt.trade_pnl("long", 100.0, 98.0, 98.0, 10, slip_bps=0.0,
                     fee_fn=lambda *a, **k: 0.0)
    check("long stop R exact", approx(q["r"], -1.0), q)

    # risk 2, reward 2 -> +1.0R
    t = bt.trade_pnl("long", 100.0, 102.0, 98.0, 10, slip_bps=0.0,
                     fee_fn=lambda *a, **k: 0.0)
    check("long 1R", approx(t["r"], 1.0), t)
    # risk 2, reward 1 -> +0.5R
    t2 = bt.trade_pnl("long", 100.0, 101.0, 98.0, 10, slip_bps=0.0,
                      fee_fn=lambda *a, **k: 0.0)
    check("long 0.5R", approx(t2["r"], 0.5), t2)


def test_short_r_multiple_exact() -> None:
    """short 100, stop 102, exit 96, no slippage, no fees -> +2R."""
    p = bt.trade_pnl("short", 100.0, 96.0, 102.0, 10, slip_bps=0.0,
                     fee_fn=lambda *a, **k: 0.0)
    check("short gross", approx(p["gross"], 40.0), p)
    check("short R exact", approx(p["r"], 2.0), p)
    q = bt.trade_pnl("short", 100.0, 102.0, 102.0, 10, slip_bps=0.0,
                     fee_fn=lambda *a, **k: 0.0)
    check("short stop R exact", approx(q["r"], -1.0), q)


def test_fees_from_repo_model() -> None:
    """trade_pnl must charge exactly fees.round_trip_equity_fees()."""
    p = bt.trade_pnl("long", 600.0, 605.0, 595.0, 20, slip_bps=0.0)
    expect = fees.round_trip_equity_fees(20, buy_price=600.0, sell_price=605.0)
    check("fee == fees.py", approx(p["fees"], expect), (p["fees"], expect))
    check("net = gross - fees", approx(p["net"], p["gross"] - p["fees"]), p)
    check("R = net / risk", approx(p["r"], p["net"] / p["risk_dollars"]), p)
    # with real (tiny) fees a 1R stop-out is slightly worse than -1R
    q = bt.trade_pnl("long", 600.0, 595.0, 595.0, 20, slip_bps=0.0)
    check("stop-out worse than -1R after fees", q["r"] < -1.0, q)


def test_slippage_deduction() -> None:
    """1 bp per side must move the entry up and the exit down for a long, and
    the hit to R must equal the modelled cost."""
    slip = 10.0 / 10_000.0  # 10 bp, exaggerated so the assertion is sharp
    p = bt.trade_pnl("long", 100.0, 110.0, 95.0, 100, slip_bps=10.0,
                     fee_fn=lambda *a, **k: 0.0)
    check("slipped entry", approx(p["entry_tx"], 100.0 * (1 + slip)), p)
    check("slipped exit", approx(p["exit_tx"], 110.0 * (1 - slip)), p)
    no_slip = bt.trade_pnl("long", 100.0, 110.0, 95.0, 100, slip_bps=0.0,
                           fee_fn=lambda *a, **k: 0.0)
    expected_cost = (100.0 * slip + 110.0 * slip) * 100
    check("slippage cost exact",
          approx(no_slip["gross"] - p["gross"], expected_cost),
          (no_slip["gross"], p["gross"], expected_cost))
    # risk is measured from the slipped entry, so it grows by the entry slippage
    check("risk from slipped entry",
          approx(p["risk_dollars"], (100.0 * (1 + slip) - 95.0) * 100), p)

    # zero-slippage scale factor: a 2R winner with 10 bp slippage is less than 2R
    check("slippage reduces R", p["r"] < 2.0, p)


def test_limit_fill_gets_no_slippage() -> None:
    p = bt.trade_pnl("long", 100.0, 110.0, 95.0, 10, slip_bps=10.0,
                     limit_fill=True, fee_fn=lambda *a, **k: 0.0)
    check("limit exit at target price", approx(p["exit_tx"], 110.0), p)
    check("limit entry still slipped", approx(p["entry_tx"], 100.1), p)


def test_size_shares() -> None:
    # $10k x 1% / $2 risk-per-share = 50 shares
    check("size 50", bt.size_shares(100.0, 98.0, 10_000.0, 0.01) == 50)
    check("size floors", bt.size_shares(100.0, 97.5, 10_000.0, 0.01) == 40)
    check("size min 1", bt.size_shares(100.0, 0.01, 10_000.0, 0.01) == 1)
    check("size zero risk", bt.size_shares(100.0, 100.0, 10_000.0, 0.01) == 0)


# --------------------------------------------------------------------------- #
# 2. exit priority
# --------------------------------------------------------------------------- #
def _one_trade(bars, stop, exit_from=2, max_hold=None, target=None):
    """Run the engine with a signal on bar 1 and assert exactly one trade."""
    sigs = {1: {"side": "long", "stop": stop, "target": target}}
    res = bt.simulate(bars, "TEST", "SYN", lambda i, b: sigs.get(i),
                      exit_close_fn=(lambda i, b, pos: i >= exit_from),
                      max_hold=max_hold, slip_bps=0.0)
    assert len(res["trades"]) == 1, res["trades"]
    return res["trades"][0]


def test_stop_wins_over_exit_rule_same_bar() -> None:
    """Bar 2 dips through the stop AND closes above the exit level: the stop must
    be taken (documented assumption - OHLC gives no intra-bar order)."""
    bars = [bar("2026-01-01", 100, 101, 99, 100),
            bar("2026-01-04", 100, 101, 99, 100),          # entry at close 100
            bar("2026-01-05", 100, 108, 97, 107)]          # low 97 < stop 98, close 107
    t = _one_trade(bars, stop=98.0)
    check("stop reason wins", t["reason"] == "stop", t)
    check("stop price used", approx(t["exit_px"], 98.0), t)
    check("stop is a loss", t["r"] < 0, t)


def test_stop_priority_reverse_order_not_needed() -> None:
    """Same bar, but the stop is NOT touched: the close-based exit must fire."""
    bars = [bar("2026-01-01", 100, 101, 99, 100),
            bar("2026-01-04", 100, 101, 99, 100),
            bar("2026-01-05", 100, 108, 99.5, 107)]        # low 99.5 > stop 98
    t = _one_trade(bars, stop=98.0)
    check("exit_rule fires", t["reason"] == "exit_rule", t)
    check("exit at close", approx(t["exit_px"], 107.0), t)
    check("winner", t["r"] > 0, t)


def test_gap_through_stop_fills_at_open() -> None:
    """A bar that opens below the stop fills at the OPEN (worse than the stop)."""
    bars = [bar("2026-01-01", 100, 101, 99, 100),
            bar("2026-01-04", 100, 101, 99, 100),          # entry 100, stop 98
            bar("2026-01-05", 90, 95, 88, 94)]             # gaps to 90
    t = _one_trade(bars, stop=98.0)
    check("gap fills at open", approx(t["exit_px"], 90.0), t)
    check("gap loss worse than -1R", t["r"] < -1.0, t)


def test_target_before_exit_rule() -> None:
    """When the target is untouched and the exit rule fires, the rule exits."""
    bars = [bar("2026-01-01", 100, 101, 99, 100),
            bar("2026-01-04", 100, 101, 99, 100),
            bar("2026-01-05", 100, 105, 99.5, 104)]
    t = _one_trade(bars, stop=98.0, target=106.0)
    check("exit_rule on untouched target", t["reason"] == "exit_rule", t)
    t2 = _one_trade(bars, stop=98.0, target=103.0)
    check("target fills when touched", t2["reason"] == "target", t2)
    check("target price exact", approx(t2["exit_px"], 103.0), t2)
    check("target is a limit fill", t2["limit_fill"] is True, t2)


def test_max_hold_time_stop() -> None:
    """With no stop/exit hit, the 10-bar max hold closes the trade at the close."""
    bars = [bar("2026-01-01", 100, 101, 99, 100)]
    for i in range(1, 14):
        bars.append(bar(f"2026-01-{4 + i:02d}", 100, 100.5, 99.5, 100.2))
    sigs = {1: {"side": "long", "stop": 50.0, "target": None}}
    res = bt.simulate(bars, "TEST", "SYN", lambda i, b: sigs.get(i),
                      exit_close_fn=lambda i, b, pos: False, max_hold=10,
                      slip_bps=0.0)
    t = res["trades"][0]
    check("max_hold reason", t["reason"] == "max_hold", t)
    check("max_hold after 10 bars", t["hold_bars"] == 10, t)
    check("max_hold exits at close", approx(t["exit_px"], 100.2), t)


def test_eod_flat_flags_open_position() -> None:
    bars = [bar("2026-01-01", 100, 101, 99, 100),
            bar("2026-01-04", 100, 101, 99, 100),
            bar("2026-01-05", 100, 101, 99, 100.5)]
    t = _one_trade(bars, stop=90.0, exit_from=999)
    check("eod_flat reason", t["reason"] == "eod_flat", t)


def test_no_reentry_while_position_open() -> None:
    """Two signals, one position: the second signal is skipped, not stacked."""
    bars = [bar("2026-01-01", 100, 101, 99, 100),
            bar("2026-01-04", 100, 101, 99, 100),
            bar("2026-01-05", 100, 101, 99, 100),
            bar("2026-01-06", 100, 101, 99, 100)]
    sigs = {1: {"side": "long", "stop": 90.0, "target": None},
            2: {"side": "long", "stop": 90.0, "target": None}}
    res = bt.simulate(bars, "TEST", "SYN", lambda i, b: sigs.get(i),
                      exit_close_fn=lambda i, b, pos: False, slip_bps=0.0)
    check("one position at a time", len(res["trades"]) == 1, res["trades"])


# --------------------------------------------------------------------------- #
# 3. indicators, sizing of risk, regime labels
# --------------------------------------------------------------------------- #
def test_indicators() -> None:
    closes = [float(i) for i in range(1, 31)]
    check("sma warmup None", bt.sma(closes, 5)[3] is None)
    check("sma value", bt.sma(closes, 5)[4] == 3.0)
    check("sma of flat", bt.sma([10.0] * 10, 3)[2] == 10.0)
    # monotone up -> RSI 100
    r = bt.rsi(closes, 2)
    check("rsi warmup None", r[1] is None)
    check("rsi up = 100", approx(r[2], 100.0), r[2])
    # monotone down -> RSI 0
    r2 = bt.rsi(list(reversed(closes)), 2)
    check("rsi down = 0", approx(r2[2], 0.0), r2[2])
    # Wilder ATR on a constant 2-point range = 2
    bars = [bar("2026-01-01", 100, 101, 99, 100) for _ in range(20)]
    check("atr constant range", approx(bt.atr(bars, 14)[-1], 2.0), bt.atr(bars, 14)[-1])


def test_regime_labels() -> None:
    """A tight, flat series is chop; a wide-ranging series is trend."""
    flat = [bar(f"2026-01-{i:02d}", 100, 100.2, 99.8, 100.0) for i in range(1, 26)]
    lab = bt.regime_series(flat, 0.04, 0.02)
    check("flat = chop", lab[-1] == "chop", lab[-1])
    wide = [bar(f"2026-01-{i:02d}", 100, 100 + 10 * (i % 3), 100 - 10 * (i % 3), 100 + 5 * (i % 2))
            for i in range(1, 26)]
    lab2 = bt.regime_series(wide, 0.04, 0.02)
    check("wide = trend", lab2[-1] == "trend", lab2[-1])
    check("warmup = na", bt.regime_series(flat, 0.04, 0.02)[0] == "na")


# --------------------------------------------------------------------------- #
# 4. the live rule geometries
# --------------------------------------------------------------------------- #
def test_orb_5min_geometry() -> None:
    """OR 100-102 (bars 09:30-10:00); the 10:05 bar closes 103 -> long at 103,
    stop 100 (opposite side), target 103 + 1.5*2 = 106."""
    session = [bar("2026-01-05", 101, 102, 100.5, 101.5, "09:30"),
               bar("2026-01-05", 101.5, 102, 100.0, 100.5, "09:35"),
               bar("2026-01-05", 100.5, 100.9, 100.2, 100.8, "09:40"),
               bar("2026-01-05", 100.8, 101.0, 100.4, 100.9, "09:45"),
               bar("2026-01-05", 100.9, 101.2, 100.6, 101.0, "09:50"),
               bar("2026-01-05", 101.0, 101.5, 100.9, 101.2, "09:55"),
               bar("2026-01-05", 101.2, 101.4, 100.8, 101.0, "10:00"),  # 7th OR bar
               bar("2026-01-05", 101.0, 101.5, 100.9, 101.1, "10:05"),  # no break
               bar("2026-01-05", 101.1, 103.5, 101.0, 103.0, "10:10")]  # closes 103 -> long
    flat = session
    sessions = {"2026-01-05": flat}
    sigs, diag = bt.build_orb_5min(sessions, flat)
    check("one ORB signal", len(sigs) == 1, sigs)
    idx, sig = next(iter(sigs.items()))
    check("ORB entry bar is the close-confirm bar", flat[idx]["hm"] == "10:10")
    check("ORB long side", sig["side"] == "long", sig)
    check("ORB stop = OR low", approx(sig["stop"], 100.0), sig)
    check("ORB target = entry + 1.5 x range",
          approx(sig["target"], 103.0 + 1.5 * 2.0), sig)
    check("ORB range recorded", approx(diag["2026-01-05"]["rng"], 2.0))

    # short side: a close below the OR low
    s2 = [dict(b) for b in session]
    s2[-1] = bar("2026-01-05", 101.1, 101.2, 98.5, 99.0, "10:10")
    sigs2, _ = bt.build_orb_5min({"2026-01-05": s2}, s2)
    idx2, sg2 = next(iter(sigs2.items()))
    check("ORB short side", sg2["side"] == "short", sg2)
    check("ORB short stop = OR high", approx(sg2["stop"], 102.0), sg2)
    check("ORB short target below entry",
          approx(sg2["target"], 99.0 - 1.5 * 2.0), sg2)


def test_pullback_geometry() -> None:
    """Every pullback signal must be exactly 1x ATR to the stop and 1.5x ATR to
    the target, measured from the trigger close."""
    bars = [bar(f"2026-01-{i:02d}", 100, 101, 99, 100.0) for i in range(1, 41)]
    bars += [bar(f"2026-02-{i:02d}", 100, 102, 98, 100.0) for i in range(1, 41)]
    bars += [bar(f"2026-03-{i:02d}", 100, 101, 99, 100.0 + (i % 3) * 0.1)
             for i in range(1, 41)]
    sigs, _, diag = bt.build_pullback(bars)
    check("pullback warmup respected", diag["warmup_days"] >= 20, diag["warmup_days"])
    check("pullback signals exist", len(sigs) > 0, len(sigs))
    a14 = bt.atr(bars, 14)
    for i, s in sigs.items():
        c = bars[i]["c"]
        a = a14[i]
        if s["side"] == "long":
            check(f"pb long stop 1ATR @{i}", approx(s["stop"], c - a), (i, s, a))
            check(f"pb long target 1.5ATR @{i}", approx(s["target"], c + 1.5 * a), (i, s, a))
        else:
            check(f"pb short stop 1ATR @{i}", approx(s["stop"], c + a), (i, s, a))


def test_mr_geometry_and_filters() -> None:
    """MR signals only fire in an uptrend (close > 200d SMA and 50d SMA rising),
    with the stop 2.5x ATR below the entry close, and RSI(2) < 10 for rsi2."""
    closes = [100 + i * 0.5 for i in range(230)]
    bars = []
    for i, c in enumerate(closes):
        bars.append(bar(f"2020-{(i // 21) + 1:02d}-{(i % 21) + 1:02d}", c, c + 1, c - 1, c))
    # a sharp 3-day dip in a clear uptrend should trip RSI(2) < 10
    for k, drop in enumerate((6.0, 6.0, 6.0)):
        i = 227 + k
        bars[i] = bar(bars[i]["d"], bars[i - 1]["c"], bars[i - 1]["c"] + 0.5,
                      bars[i - 1]["c"] - drop, bars[i - 1]["c"] - drop)
    sigs, exits = bt.build_mr_rsi2(bars)
    check("mr_rsi2 fired on the dip", len(sigs) >= 1, len(sigs))
    r2 = bt.rsi([b["c"] for b in bars], 2)
    a14 = bt.atr(bars, 14)
    for i, s in sigs.items():
        check(f"mr_rsi2 RSI<10 @{i}", r2[i] < bt.RSI2_ENTRY, (i, r2[i]))
        check(f"mr_rsi2 long only @{i}", s["side"] == "long", s)
        check(f"mr_rsi2 stop = close - 2.5ATR @{i}",
              approx(s["stop"], bars[i]["c"] - 2.5 * a14[i]), (i, s, a14[i]))
        check(f"mr_rsi2 has no target @{i}", s["target"] is None, s)
    # the dip bar itself must not be an exit bar (close < 5d SMA during a drop)
    check("no exit on the dip bar", all(not exits.get(i) for i in sigs), sigs)


def test_mr_dip_geometry() -> None:
    closes = [100 + i * 0.5 for i in range(230)]
    bars = [bar(f"2019-{(i // 21) + 1:02d}-{(i % 21) + 1:02d}", c, c + 1, c - 1, c)
            for i, c in enumerate(closes)]
    i = 229
    bars[i] = bar(bars[i]["d"], closes[i - 1], closes[i - 1] + 0.5,
                  closes[i - 1] - 8.0, closes[i - 1] - 8.0)
    sigs, exits = bt.build_mr_atr_dip(bars)
    check("mr_dip fired", i in sigs, list(sigs))
    a14 = bt.atr(bars, 14)
    s10 = bt.sma([b["c"] for b in bars], 10)
    check("mr_dip trigger = close < sma10 - 2ATR",
          bars[i]["c"] < s10[i] - 2.0 * a14[i], (bars[i]["c"], s10[i], a14[i]))
    check("mr_dip stop 2.5ATR", approx(sigs[i]["stop"], bars[i]["c"] - 2.5 * a14[i]))


# --------------------------------------------------------------------------- #
# 5. metrics
# --------------------------------------------------------------------------- #
def test_metrics_helpers() -> None:
    check("consec losers", bt.max_consec_losers([1, -1, -1, -1, 2, -1]) == 3)
    check("consec losers all win", bt.max_consec_losers([1, 2, 3]) == 0)
    trades = [{"r": r, "exit_date": f"2026-01-{i:02d}", "symbol": "X",
               "hold_bars": 3, "reason": "max_hold"}
              for i, r in enumerate([1.0, -1.0, -2.0, 1.0], start=1)]
    check("max DD in R", approx(bt.max_dd_r(trades), 3.0), bt.max_dd_r(trades))
    s = bt.summarize(trades)
    check("summarize n", s["n"] == 4)
    check("summarize total R", approx(s["total_r"], -1.0), s["total_r"])
    check("summarize expectancy", approx(s["exp_r"], -0.25), s["exp_r"])
    check("summarize win rate", approx(s["win_rate"], 0.5), s["win_rate"])
    check("summarize empty", bt.summarize([])["n"] == 0)


def test_bootstrap_ci() -> None:
    ci = bt.bootstrap_ci([1.0] * 50, iters=200)
    check("bootstrap deterministic", ci is not None and approx(ci[0], 1.0) and approx(ci[1], 1.0), ci)
    check("bootstrap needs n>=5", bt.bootstrap_ci([1.0, 2.0]) is None)


def test_costs_matter_sign_of_edge() -> None:
    """A 1R winner set can be wiped out by costs: this pins that the accounting
    actually subtracts them rather than decorating the result."""
    gross = bt.trade_pnl("long", 600.0, 601.0, 599.0, 5, slip_bps=0.0,
                         fee_fn=lambda *a, **k: 0.0)
    net = bt.trade_pnl("long", 600.0, 601.0, 599.0, 5, slip_bps=bt.SLIP_BPS)
    check("gross positive", gross["r"] > 0, gross)
    check("net smaller", net["r"] < gross["r"], (gross["r"], net["r"]))


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}  ({len(PASSED)} assertions so far)")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} test functions passed, "
          f"{len(PASSED)} individual assertions checked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
