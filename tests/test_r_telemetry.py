"""Tests for r_telemetry — the MFE/MAE + stop-distance measurement layer.

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_r_telemetry.py

Plain asserts, no pytest (the container has no third-party packages).

What must be right, and why each one is pinned here:
  * MFE/MAE are measured ONLY between the entry and the exit. A bar after the
    exit (or before the entry) must not move either number — measuring a trade
    with bars it did not live through is the exact error that once produced a
    trade with MFE +3.28R AND MAE +3.39R off a 0.26%-wide stop.
  * A gap through the stop must show up as mae < -1R (the -1.74R signature).
  * backfill must be IDEMPOTENT and must not re-probe a row it already filled —
    reconcile runs every 10 minutes and a permanent re-fetch loop would be a
    self-inflicted outage.
  * 'not measured' must be distinguishable from 'measured and fine', or the
    nightly review reads a missing column as a clean exit record.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import db                                              # noqa: E402
import r_telemetry                                     # noqa: E402
import self_improve                                    # noqa: E402

FAILS: list[str] = []
SENTINEL = "__rtelemtest__"


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def _clean() -> None:
    """Clear this file's sentinel rows FIRST.

    A re-runnable test that inherits rows from its own earlier run fails for
    reasons that have nothing to do with the code (that already cost this repo
    one triage on tests/test_guards.py).
    """
    conn = db.connect()
    try:
        conn.execute("DELETE FROM trades WHERE account=?", (SENTINEL,))
        conn.commit()
    finally:
        conn.close()


class FakeBars:
    """Stands in for AlpacaClient.bars, INCLUDING the free-tier truncation quirk.

    The real endpoint applies `start`/`end` and then returns the FIRST `limit`
    bars of that window (ascending) — dropping the most recent bars when the
    window holds more than `limit`. A fake that ignores `limit` cannot catch the
    bug that made four measurable trades come back as "unmeasurable", so this one
    reproduces it faithfully.
    """

    def __init__(self, five: list, daily: list):
        self.five, self.daily = five, daily
        self.calls: list[tuple] = []

    def bars(self, symbol, timeframe="1Day", limit=100, start=None, end=None, **kw):
        self.calls.append((symbol, timeframe, limit, start, end))
        series = self.five if timeframe == "5Min" else self.daily
        out = []
        for b in series:
            d = str(b["t"])[:10]
            if start and d < start:
                continue
            if end and d > end:
                continue
            out.append(b)
        return {"bars": out[:limit], "symbol": symbol}


def _b(ts: str, h: float, l: float, c: float | None = None) -> dict:
    return {"t": ts, "o": l, "h": h, "l": l, "c": c if c is not None else (h + l) / 2, "v": 1}


def test_long_excursions_and_window() -> None:
    print("long trade: MFE/MAE in R, and bars outside the holding window are ignored")
    t = {"symbol": "ZZZ", "side": "long", "entry_price": 100.0, "stop_price": 98.0, "qty": 10}
    bars = [
        _b("2026-09-10T13:00:00Z", 130.0, 120.0),   # BEFORE entry — must be ignored
        _b("2026-09-10T13:35:00Z", 102.0, 99.5),
        _b("2026-09-10T13:40:00Z", 105.0, 99.0),   # best high during the trade
        _b("2026-09-10T13:45:00Z", 101.0, 98.5),   # worst low during the trade
        _b("2026-09-10T14:00:00Z", 140.0, 95.0),   # AFTER exit — must be ignored
    ]
    c = FakeBars(bars, [])
    ex = r_telemetry.excursions(c, t, r_telemetry._ts("2026-09-10T13:35:00Z"),
                               r_telemetry._ts("2026-09-10T13:50:00Z"))
    check("mfe uses the in-window high only", abs(ex["mfe_r"] - (105.0 - 100.0) / 2.0) < 1e-9,
          f"mfe_r={ex['mfe_r']}")
    check("mae uses the in-window low only", abs(ex["mae_r"] - (98.5 - 100.0) / 2.0) < 1e-9,
          f"mae_r={ex['mae_r']}")
    check("only 3 bars counted (out-of-window bars dropped)", ex["bars"] == 3, f"{ex['bars']}")
    check("a 5-min request is what was made", c.calls[0][1] == "5Min", str(c.calls[0]))


def test_short_excursions() -> None:
    print("short trade: favourable is DOWN, adverse is UP")
    t = {"symbol": "ZZZ", "side": "short", "entry_price": 100.0, "stop_price": 102.0, "qty": 5}
    bars = [_b("2026-09-10T13:35:00Z", 101.5, 96.0)]
    c = FakeBars(bars, [])
    ex = r_telemetry.excursions(c, t, r_telemetry._ts("2026-09-10T13:00:00Z"),
                               r_telemetry._ts("2026-09-10T14:00:00Z"))
    check("short mfe is the LOWER excursion", abs(ex["mfe_r"] - (100.0 - 96.0) / 2.0) < 1e-9,
          f"mfe_r={ex['mfe_r']}")
    check("short mae is the HIGHER excursion", abs(ex["mae_r"] - (100.0 - 101.5) / 2.0) < 1e-9,
          f"mae_r={ex['mae_r']}")


def test_gap_through_stop() -> None:
    print("a gap through the stop shows as mae worse than -1R (the -1.74R signature)")
    t = {"symbol": "SLV", "side": "long", "entry_price": 60.89, "stop_price": 58.1404, "qty": 19}
    risk = 60.89 - 58.1404
    risky_low = 60.89 - 1.74 * risk
    bars = [_b("2026-09-23T13:35:00Z", 60.9, risky_low)]
    c = FakeBars(bars, [])
    ex = r_telemetry.excursions(c, t, r_telemetry._ts("2026-09-23T13:30:00Z"),
                               r_telemetry._ts("2026-09-23T14:00:00Z"))
    check("mae below -1R for a gap-through", ex["mae_r"] < -1.0, f"mae_r={ex['mae_r']}")
    check("mae matches the arithmetic", abs(ex["mae_r"] + 1.74) < 1e-3, f"mae_r={ex['mae_r']}")


def test_stop_atr() -> None:
    print("stop distance in ATR units (daily bars up to the entry session)")
    t = {"symbol": "ZZZ", "side": "long", "entry_price": 100.0, "stop_price": 96.0, "qty": 1}
    # 20 flat-range sessions of 2.0 -> ATR(14) == 2.0 exactly, so risk 4.0 = 2.0 ATR.
    daily = [_b(f"2026-08-{d:02d}T20:00:00Z", 101.0, 99.0, 100.0) for d in range(1, 21)]
    c = FakeBars([], daily)
    v = r_telemetry.stop_atr(c, t, r_telemetry._ts("2026-09-10T13:35:00Z"))
    check("stop_atr == risk / ATR", v is not None and abs(v - 2.0) < 1e-6, f"{v}")
    check("daily bars requested, never 5-min, for the ATR",
          c.calls[-1][1] == "1Day", str(c.calls[-1]))


def test_flat_range_gives_expected_atr() -> None:
    print("ATR sanity: a constant 2.0 range series yields ATR 2.0 (guards an indicator swap)")
    import indicators
    daily = [_b(f"2026-08-{d:02d}T20:00:00Z", 101.0, 99.0, 100.0) for d in range(1, 21)]
    check("ATR == 2.0", abs(indicators.atr(daily, 14)[-1] - 2.0) < 1e-9,
          str(indicators.atr(daily, 14)[-1]))


def test_entry_ts_fallback() -> None:
    print("entry timestamp falls back to created_at (pre-Sep-17 rows have no entry_time)")
    t = {"entry_time": None, "created_at": "2026-09-09T16:33:12.442745+00:00"}
    check("created_at used when entry_time is null",
          r_telemetry.entry_ts(t).isoformat().startswith("2026-09-09T16:33:12"), "")
    t2 = {"entry_time": "2026-09-17T15:24:07+00:00", "created_at": "2026-01-01T00:00:00+00:00"}
    check("entry_time wins when present",
          r_telemetry.entry_ts(t2).isoformat().startswith("2026-09-17"), "")


def test_backfill_idempotent() -> None:
    print("backfill is idempotent and does not re-probe a filled row")
    db.init_db()
    risk = 2.0
    tid = db.insert_trade(
        account=SENTINEL, strategy="test_book", symbol="ZZZ", asset_class="etf",
        side="long", qty=1.0, entry_price=100.0, stop_price=98.0, status="closed",
        exit_price=99.0, net_pnl=-1.0, fees=0.0, close_reason="market",
        entry_time="2026-09-10T13:35:00Z", exit_time="2026-09-10T13:50:00Z")
    bars5 = [_b("2026-09-10T13:40:00Z", 101.0, 99.0)]
    daily = [_b(f"2026-08-{d:02d}T20:00:00Z", 101.0, 99.0, 100.0) for d in range(1, 21)]
    c = FakeBars(bars5, daily)
    n1 = r_telemetry.backfill(SENTINEL, client=c, verbose=False)
    check("first pass fills the row", n1 == 1, f"filled={n1}")
    row = [t for t in db.all_trades(SENTINEL) if t["id"] == tid][0]
    check("mfe_r persisted", row["mfe_r"] is not None, f"{row['mfe_r']}")
    check("stop_atr persisted", row["stop_atr"] is not None, f"{row['stop_atr']}")
    check("mfe/mae consistent with the risk unit",
          abs(row["mfe_r"] - (101.0 - 100.0) / risk) < 1e-9 and
          abs(row["mae_r"] - (99.0 - 100.0) / risk) < 1e-9,
          f"mfe={row['mfe_r']} mae={row['mae_r']}")
    calls_after_first = len(c.calls)
    n2 = r_telemetry.backfill(SENTINEL, client=c, verbose=False)
    check("second pass is a no-op", n2 == 0, f"filled={n2}")
    check("second pass made NO feed requests", len(c.calls) == calls_after_first,
          f"{len(c.calls)} vs {calls_after_first}")
    # A row the feed cannot measure must not be re-probed every tick either.
    tid2 = db.insert_trade(
        account=SENTINEL, strategy="test_book", symbol="OLD", asset_class="etf",
        side="long", qty=1.0, entry_price=100.0, stop_price=95.0, status="closed",
        exit_price=99.0, net_pnl=-1.0, fees=0.0, close_reason="market",
        entry_time="2026-01-01T13:35:00Z", exit_time="2026-01-02T13:50:00Z")
    c2 = FakeBars([], [])                      # no data: unmeasurable
    r_telemetry.backfill(SENTINEL, client=c2, verbose=False)
    check("unmeasurable row records an attempt stamp",
          [t for t in db.all_trades(SENTINEL) if t["id"] == tid2][0]["telemetry_at"] is not None,
          "")
    c3 = FakeBars([], [])
    r_telemetry.backfill(SENTINEL, client=c3, verbose=False)
    check("and is not re-probed inside the retry window", len(c3.calls) == 0, f"{len(c3.calls)}")
    c4 = FakeBars([], [])
    r_telemetry.backfill(SENTINEL, client=c4, force=True, verbose=False)
    check("force=True re-probes", len(c4.calls) > 0, f"{len(c4.calls)}")
    _clean()


def test_stats_and_not_measured() -> None:
    print("bucket stats: capture, MFE>=1R, and 'not measured' vs 'no defect'")
    rows = [
        {"net_pnl": -20.0, "qty": 1.0, "entry_price": 100.0, "stop_price": 90.0,
         "mfe_r": 1.6, "mae_r": -1.0, "stop_atr": 0.4},
        {"net_pnl": -10.0, "qty": 1.0, "entry_price": 100.0, "stop_price": 90.0,
         "mfe_r": 1.2, "mae_r": -1.2, "stop_atr": 0.5},
        {"net_pnl": 5.0, "qty": 1.0, "entry_price": 100.0, "stop_price": 90.0,
         "mfe_r": 0.5, "mae_r": -0.3, "stop_atr": 0.6},
    ]
    s = r_telemetry._stats(rows)
    check("total R = sum(net)/risk", abs(s["total_R"] - (-2.5)) < 1e-9, f"{s['total_R']}")
    check("mfe_ge_1R counts only the >= 1R prints", s["mfe_ge_1R"] == 2, f"{s['mfe_ge_1R']}")
    check("capture = kept R / offered R", abs(s["capture"] - (-2.5 / 3.3)) < 1e-3,
          f"{s['capture']}")
    check("median MFE is a median, not a mean", abs(s["median_mfe_R"] - 1.2) < 1e-9,
          f"{s['median_mfe_R']}")
    # Two MFE>=1R prints against a NEGATIVE total R is an EXIT defect: the entry
    # worked and the exit gave it back. That verdict has to be readable off this.
    check("exit-defect signature is visible", s["mfe_ge_1R"] >= 2 and s["total_R"] < 0, "")
    empty = self_improve._exit_quality([{"net_pnl": 1.0, "qty": 1.0,
                                         "entry_price": 10.0, "stop_price": 9.0}])
    check("no telemetry reads as 'not measured'", empty == {"exit_telemetry": "not measured"},
          str(empty))
    filled = self_improve._exit_quality(rows)
    check("measured bucket reports capture", filled.get("capture") is not None, str(filled))


def test_truncation_retry() -> None:
    """Regression: an under-sized `limit` must not silently empty the window.

    The real free-tier endpoint returns the FIRST `limit` bars of the requested
    window. Four live trades (SMH, SLV, META, XLF) came back "unmeasurable" on
    2026-09-28 because the limit was sized off the trade's duration (1-2 days)
    while the requested span was ~6 days, so the page ended BEFORE the entry.
    Force that situation by shrinking the per-day bar estimate and assert the
    fetch still finds the in-window bars (and that it took more than one request
    to do it).
    """
    print("truncation: a full page is re-requested, not silently accepted")
    entry, exit_ = "2026-09-10T13:35:00Z", "2026-09-10T14:30:00Z"
    # 48 bars BEFORE the entry, all inside the requested span (t0-2d .. t1+1d), so
    # an under-sized page is filled entirely by pre-entry bars — the live failure.
    filler = [_b(f"2026-09-0{d}T{h:02d}:00:00Z", 200.0, 100.0)
              for d in (8, 9) for h in range(0, 24)]
    inside = [_b("2026-09-10T13:40:00Z", 105.0, 99.0), _b("2026-09-10T14:00:00Z", 101.0, 98.5)]
    series = sorted(filler + inside, key=lambda b: b["t"])
    t = {"symbol": "ZZZ", "side": "long", "entry_price": 100.0, "stop_price": 98.0, "qty": 1}
    real = r_telemetry._5MIN_PER_DAY
    try:
        r_telemetry._5MIN_PER_DAY = 1        # simulate a badly under-sized limit
        c = FakeBars(series, [])
        ex = r_telemetry.excursions(c, t, r_telemetry._ts(entry), r_telemetry._ts(exit_))
        check("in-window bars found despite a full first page", ex is not None,
              "None" if ex is None else f"mfe_r={ex['mfe_r']}")
        check("the retry widened the request", len(c.calls) > 1, f"{len(c.calls)} calls")
        check("limit grows between attempts", c.calls[-1][2] > c.calls[0][2],
              f"{c.calls[0][2]} -> {c.calls[-1][2]}")
    finally:
        r_telemetry._5MIN_PER_DAY = real


def test_capture_uses_the_measured_subset_only() -> None:
    print("capture compares realized R with MFE on the SAME trades")
    rows = [
        # measured, big loss against a +2R excursion -> exit gave it back
        {"net_pnl": -20.0, "qty": 1.0, "entry_price": 100.0, "stop_price": 90.0,
         "mfe_r": 2.0, "mae_r": -1.0, "stop_atr": 1.0},
        # NOT measured (no 5-min history) -> must be excluded from BOTH sides
        {"net_pnl": -50.0, "qty": 1.0, "entry_price": 100.0, "stop_price": 90.0,
         "mfe_r": None, "mae_r": None, "stop_atr": 1.0},
    ]
    s = r_telemetry._stats(rows)
    check("capture counts only the measured trade", s["capture_n"] == 1, str(s["capture_n"]))
    check("capture = -2R kept of +2R offered", abs(s["capture"] - (-1.0)) < 1e-9,
          f"{s['capture']}")
    check("total R still covers every trade", abs(s["total_R"] - (-7.0)) < 1e-9,
          f"{s['total_R']}")


def test_risk_per_share_edges() -> None:
    print("risk unit edges: no stop / zero-width stop never divide")
    check("null stop -> None",
          r_telemetry.risk_per_share({"entry_price": 10.0, "stop_price": None}) is None, "")
    check("zero-width stop -> None",
          r_telemetry.risk_per_share({"entry_price": 10.0, "stop_price": 10.0}) is None, "")
    check("normal stop -> distance",
          r_telemetry.risk_per_share({"entry_price": 10.0, "stop_price": 9.5}) == 0.5, "")


def main() -> int:
    db.init_db()
    _clean()
    for fn in (test_long_excursions_and_window, test_short_excursions, test_gap_through_stop,
               test_stop_atr, test_flat_range_gives_expected_atr, test_entry_ts_fallback,
               test_truncation_retry, test_capture_uses_the_measured_subset_only,
               test_backfill_idempotent, test_stats_and_not_measured,
               test_risk_per_share_edges):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
