"""Tests for the portfolio sleeve's entry rule and its new empty-scan diagnostic.

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_portfolio_sleeve.py

Why these exist
---------------
The sweep-style rule is shared with the strategy lab, so `_signal` must not drift.
The 2026-11-16 addition is `_band_gap_atr`, a stdout-only diagnostic printed when a
scan finds nothing: it answers "how far outside the band was the closest symbol?"
without touching the entry decision. These tests pin BOTH halves of that claim -
the diagnostic measures the gap correctly, and the decision path is unchanged by
its presence.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import portfolio_run                                   # noqa: E402
from indicators import atr, sma                        # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def _series(last_close: float, step: float = 0.0, n: int = 120) -> list[dict]:
    """Flat daily bars with a known last close; SMA50 ~= 100 and ATR ~= 2.

    Bars are constructed so the last 15 sessions alternate by +-1.0 around 100,
    which makes ATR14 exactly 2.0 (high-low = 2.0) and the SMA50 ~= 100.
    """
    bars = []
    for i in range(n):
        c = 100.0
        if i == n - 1:
            c = last_close
        h = c + 1.0
        lo = c - 1.0
        bars.append({"c": c, "h": h, "l": lo})
    return bars


def _edges(bars) -> tuple[float, float]:
    """(SMA50, ATR14) of the series as the sleeve computes them."""
    closes = [b["c"] for b in bars]
    s = sma(closes, portfolio_run.PARAMS["sma_n"])[-1]
    a = atr(bars, 14)[-1]
    assert s is not None and a is not None, "synthetic series must yield SMA/ATR"
    return s, a


def test_signal_fires_inside_the_band() -> None:
    print("close just above SMA50 => a setup with 1.5/2.5 ATR geometry")
    bars = _series(100.5)
    s = portfolio_run._signal(bars)
    check("setup found", s is not None)
    if not s:
        return
    a = s["atr"]
    check("stop = entry - 1.5 ATR", abs(s["stop"] - (s["entry"] - 1.5 * a)) < 1e-9, str(s["stop"]))
    check("target = entry + 2.5 ATR", abs(s["target"] - (s["entry"] + 2.5 * a)) < 1e-9, str(s["target"]))
    check("inside-band gap reads zero", portfolio_run._band_gap_atr(bars) == 0.0)


def test_signal_refuses_above_the_band() -> None:
    print("close more than 1 ATR above SMA50 => no setup, positive gap")
    base = _series(100.0)
    sma_v, atr_v = _edges(base)
    px = sma_v + (portfolio_run.PARAMS["band"] + 1.0) * atr_v   # ~1 ATR past the edge
    bars = base[:-1] + [{"c": px, "h": px + 1.0, "l": px - 1.0}]
    check("no setup", portfolio_run._signal(bars) is None)
    g = portfolio_run._band_gap_atr(bars)
    s2, a2 = _edges(bars)
    expect = (px - s2 - portfolio_run.PARAMS["band"] * a2) / a2
    check("gap measured above the band", g is not None and g > 0 and abs(g - expect) < 1e-6,
          f"{g} vs {expect}")


def test_signal_refuses_below_the_band() -> None:
    print("close below SMA50 => no setup, gap in the direction it must move")
    base = _series(100.0)
    sma_v, atr_v = _edges(base)
    px = sma_v - 1.0 * atr_v                                   # ~1 ATR below the SMA
    bars = base[:-1] + [{"c": px, "h": px + 1.0, "l": px - 1.0}]
    check("no setup", portfolio_run._signal(bars) is None)
    g = portfolio_run._band_gap_atr(bars)
    s2, a2 = _edges(bars)
    expect = (s2 - px) / a2
    check("gap measured below the band", g is not None and g > 0 and abs(g - expect) < 1e-6,
          f"{g} vs {expect}")


def test_gap_is_bounded_and_never_negative() -> None:
    print("gap >= 0 for every price, and 0 exactly inside the band")
    base = _series(100.0)
    sma_v, atr_v = _edges(base)
    lo_edge, hi_edge = sma_v, sma_v + portfolio_run.PARAMS["band"] * atr_v
    for px in (sma_v - 2 * atr_v, sma_v - 0.5 * atr_v, lo_edge + 0.01,
               (lo_edge + hi_edge) / 2, hi_edge - 0.01, hi_edge + 0.5 * atr_v):
        bars = base[:-1] + [{"c": px, "h": px + 1.0, "l": px - 1.0}]
        g = portfolio_run._band_gap_atr(bars)
        if g is None:
            check(f"px {px:.2f}: gap returned", False)
            continue
        inside = lo_edge < px <= hi_edge
        ok = g >= -1e-3 and (abs(g) < 1e-3 if inside else g > 0)
        check(f"px {px:.2f}: gap {g:.3f} inside={inside}", ok)


def test_missing_history_yields_no_gap() -> None:
    print("too few bars => None (no guessing)")
    bars = [{"c": 100.0, "h": 101.0, "l": 99.0} for _ in range(10)]
    check("no gap without 50+ bars", portfolio_run._band_gap_atr(bars) is None)
    check("no signal without 50+ bars", portfolio_run._signal(bars) is None)


def main() -> int:
    print(f"config VERSION {config.VERSION}; PARAMS {portfolio_run.PARAMS}")
    for fn in (test_signal_fires_inside_the_band, test_signal_refuses_above_the_band,
               test_signal_refuses_below_the_band, test_gap_is_bounded_and_never_negative,
               test_missing_history_yields_no_gap):
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("all portfolio sleeve tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
