"""Tests for market_ctx — synthetic fixtures, hand-computed expectations.

Run: python3 tests/test_market_ctx.py   (plain asserts, no pytest, no network)

Everything here is offline by construction: the Alpaca client is faked and the
only two network paths in the module (CBOE VIX, Nasdaq earnings) are
monkeypatched. The pinned numbers are hand-derived, not captured from a run —
see the arithmetic in each docstring/comment so a future reader can re-derive
them instead of trusting the test:

  * ATR14        14 flat TR=2 sessions then one TR=4 session
                 -> 2.0 then (2*13 + 4)/14 = 30/14 = 2.142857142857143
  * RSI14        alternating +2/-1 for 14 deltas -> avg gain 1.00, avg loss 0.50
                 -> RS 2 -> 100 - 100/3 = 200/3 = 66.6666...
                 one more -1 delta: avg gain 13/14, avg loss 15/28
                 -> RS 26/15 -> 100 - 1500/41 = 2600/41 = 63.4146341...
  * SMA20        closes 1..20 -> mean = 210/20 = 10.5
  * range_pos    20d low 95, 20d high 105, last 100 -> 5/10 = 50.0
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_ctx as mc  # noqa: E402  (path set above)

ET = ZoneInfo("America/New_York")
TESTS: list = []


def test(fn):
    TESTS.append(fn)
    return fn


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def daily_bars(closes, highs=None, lows=None, vols=None, end: date | None = None):
    """Daily bars whose LAST bar is `end` (default yesterday) — so no bar is
    today's in-progress bar and the module treats every one as completed."""
    end = end or (datetime.now(ET).date() - timedelta(days=1))
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        d = end - timedelta(days=n - 1 - i)
        out.append({
            "t": f"{d.isoformat()}T20:00:00Z",
            "o": c,
            "h": highs[i] if highs else c + 1.0,
            "l": lows[i] if lows else c - 1.0,
            "c": c,
            "v": vols[i] if vols else 100.0,
            "n": 1,
            "vw": c,
        })
    return out


def intraday_bars(rows, day: date | None = None):
    """Today's 5Min bars. `rows` = [(hh, mm, o, h, l, c, v), ...] in ET wall clock."""
    day = day or datetime.now(ET).date()
    out = []
    for hh, mm, o, h, l, c, v in rows:
        dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)
        out.append({
            "t": dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": o, "h": h, "l": l, "c": c, "v": v, "n": 1, "vw": c,
        })
    return out


class FakeClient:
    """Minimal duck-typed stand-in for alpaca_rest.AlpacaClient (reads only)."""

    def __init__(self, daily=None, five=None, prices=None, boom=False):
        self.daily = daily or {}
        self.five = five or {}
        self.prices = prices or {}
        self.boom = boom
        self.calls: list[tuple[str, str]] = []

    def bars(self, symbol, timeframe="1Day", limit=100, start=None, end=None,
             adjustment="all"):
        if self.boom:
            raise RuntimeError("data host down")
        self.calls.append((symbol, timeframe))
        src = self.daily if timeframe == "1Day" else self.five
        return {"bars": src.get(symbol)}  # None mirrors the free-tier 'bars: null'

    def latest_trade(self, symbol):
        if self.boom:
            raise RuntimeError("data host down")
        px = self.prices.get(symbol)
        return {"price": px, "time": "2026-09-17T15:00:00Z"} if px else None


def no_network(url, headers, timeout=20):
    raise ConnectionError(f"offline test: {url}")


def rsi_calls(client: FakeClient) -> int:
    """How many bars() calls used a given timeframe."""
    return len([c for c in client.calls])


# --------------------------------------------------------------------------- #
# Indicator arithmetic (hand-computed)
# --------------------------------------------------------------------------- #
@test
def test_atr14_wilder_hand_computed():
    """14 flat sessions (TR=2) then one wide session (TR=4).

    ATR[14] = mean(2 x14) = 2.0 ; ATR[15] = (2.0*13 + 4)/14 = 30/14.
    """
    closes = [100.0] * 15
    highs = [101.0] * 14 + [102.0]
    lows = [99.0] * 14 + [98.0]
    client = FakeClient(daily={"TEST": daily_bars(closes, highs=highs, lows=lows)},
                        prices={"TEST": 100.0})
    snap = mc.symbol_snapshot(client, "TEST")
    assert snap["atr14"] == round(30.0 / 14.0, 2) == 2.14, snap["atr14"]
    # atr_pct = atr14 / last * 100 = 2.142857...% of a 100.00 price
    assert snap["atr_pct"] == round(30.0 / 14.0 / 100.0 * 100.0, 2) == 2.14, snap["atr_pct"]

    # ...and the unrounded arithmetic behind it, straight off indicators.atr
    import indicators
    series = indicators.atr(daily_bars(closes, highs=highs, lows=lows), 14)
    assert series[-2] == 2.0, series[-2]
    assert abs(series[-1] - 30.0 / 14.0) < 1e-12, series[-1]


@test
def test_rsi14_wilder_hand_computed():
    """Alternating +2/-1 closes: 7 gains of 2 and 7 losses of 1 over 14 deltas.
    avg_gain = 1.0, avg_loss = 0.5 -> RS = 2 -> RSI = 100 - 100/3 = 66.6667.
    """
    closes = [10.0, 12.0, 11.0, 13.0, 12.0, 14.0, 13.0, 15.0, 14.0, 16.0,
              15.0, 17.0, 16.0, 18.0, 17.0]
    assert abs(mc._rsi_wilder(closes, 14) - 200.0 / 3.0) < 1e-9

    # One more -1 delta: avg_gain = 13/14, avg_loss = (0.5*13 + 1)/14 = 15/28
    # -> RS = 26/15 -> RSI = 100 - 100*15/41 = 2600/41 = 63.4146341...
    closes2 = closes + [16.0]
    assert abs(mc._rsi_wilder(closes2, 14) - 2600.0 / 41.0) < 1e-9

    # No losses at all -> 100 (not a divide-by-zero), flat -> 50.
    assert mc._rsi_wilder([float(i) for i in range(1, 16)], 14) == 100.0
    assert mc._rsi_wilder([100.0] * 20, 14) == 50.0
    # Not enough history -> None, never a guess.
    assert mc._rsi_wilder([1.0, 2.0, 3.0], 14) is None


@test
def test_sma_range_and_change_pct():
    """closes 1..20 -> SMA20 = 210/20 = 10.5; SMA50/200 unavailable -> None."""
    closes = [float(i) for i in range(1, 21)]
    client = FakeClient(daily={"T": daily_bars(closes)}, prices={"T": 20.0})
    snap = mc.symbol_snapshot(client, "T")
    assert snap["sma20"] == 10.5, snap["sma20"]
    assert snap["sma50"] is None and snap["sma200"] is None
    assert snap["last"] == 20.0 and snap["last_close"] == 20.0
    assert snap["chg_1d_pct"] == 0.0            # last == last_close
    assert snap["chg_5d_pct"] == round((20.0 / 15.0 - 1) * 100, 2)  # base = closes[-6]
    assert snap["chg_30d_pct"] is None          # only 20 closes: no 30d claim
    assert snap["dist_sma20_pct"] == 90.48      # (20/10.5 - 1)*100, rounded


@test
def test_today_partial_bar_conventions():
    """Pins WHICH series each field uses when today's in-progress bar exists.

    closes = 100..139 with the LAST bar stamped TODAY:
      last (live) 139, last_close (prior session) 138 -> 1d = +0.72%
      5d  = all_closes[-6]  = 134 -> (139/134-1)  = +3.73%   (today = session 0)
      30d = all_closes[-31] = 109 -> (139/109-1)  = +27.52%
      SMA20 = mean of the 20 COMPLETED closes (119..138) = 128.5, NOT 129.5
      20d range window INCLUDES today -> high 140 (139+1), low 119
    """
    closes = [100.0 + i for i in range(40)]
    bars = daily_bars(closes, end=datetime.now(ET).date())   # last bar = today
    client = FakeClient(daily={"P": bars}, prices={"P": 139.0})
    snap = mc.symbol_snapshot(client, "P")
    assert snap["last"] == 139.0 and snap["last_close"] == 138.0
    assert snap["chg_1d_pct"] == 0.72, snap["chg_1d_pct"]
    assert snap["chg_5d_pct"] == 3.73, snap["chg_5d_pct"]
    assert snap["chg_30d_pct"] == 27.52, snap["chg_30d_pct"]
    assert snap["sma20"] == 128.5, snap["sma20"]          # completed series only
    assert snap["dist_sma20_pct"] == 8.17, snap["dist_sma20_pct"]
    assert snap["atr14"] == 2.0, snap["atr14"]            # completed series only
    assert snap["high_20d"] == 140.0 and snap["low_20d"] == 119.0   # window includes today
    assert snap["range_pos_pct"] == 95.2, snap["range_pos_pct"]


@test
def test_range_pos_and_20d_extremes():
    """20d low 95 / high 105, last 100 -> (100-95)/(105-95) = 50.0%."""
    closes = [100.0] * 20
    lows = [99.0] * 20
    highs = [101.0] * 20
    lows[3] = 95.0
    highs[9] = 105.0
    client = FakeClient(daily={"R": daily_bars(closes, highs=highs, lows=lows)},
                        prices={"R": 100.0})
    snap = mc.symbol_snapshot(client, "R")
    assert snap["high_20d"] == 105.0 and snap["low_20d"] == 95.0
    assert snap["range_pos_pct"] == 50.0, snap["range_pos_pct"]

    # At the top of the range the same math reads 100 (breakout day), and a
    # last price beyond the range clamps instead of overshooting.
    client.prices["R"] = 110.0
    mc.clear_cache()
    assert mc.symbol_snapshot(client, "R")["range_pos_pct"] == 100.0


@test
def test_volume_ratio_and_sma_slopes():
    """Reference volume 150 vs the 20 sessions before it at 100 -> 1.5x.

    Rising series -> sma50_rising True; falling series -> False.
    """
    vols = [100.0] * 20 + [150.0]
    closes = [100.0 + i for i in range(21)]
    client = FakeClient(daily={"V": daily_bars(closes, vols=vols)})
    snap = mc.symbol_snapshot(client, "V")
    assert snap["vol_ratio"] == 1.5, snap["vol_ratio"]

    rising = FakeClient(daily={"U": daily_bars([100.0 + i * 0.5 for i in range(300)])})
    assert mc.symbol_snapshot(rising, "U")["sma50_rising"] is True
    mc.clear_cache()
    falling = FakeClient(daily={"D": daily_bars([300.0 - i * 0.5 for i in range(300)])})
    assert mc.symbol_snapshot(falling, "D")["sma50_rising"] is False


@test
def test_intraday_gap_orb_and_extremes():
    """Today's 5Min window: ORB = 09:30-10:00 (6 bars), gap vs prior close.

    prior close 98.00, today's open 100.00 -> gap = (100/98 - 1) = +2.04%.
    ORB high 103.0 / low 99.5; the 10:00 bar extends intraday to 104.0 / 99.5.
    """
    rows = [
        (9, 30, 100.0, 101.0, 99.5, 100.5, 10),
        (9, 35, 100.5, 102.0, 100.0, 101.5, 10),
        (9, 40, 101.5, 101.8, 101.0, 101.2, 10),
        (9, 45, 101.2, 102.5, 101.0, 102.0, 10),
        (9, 50, 102.0, 102.2, 101.5, 101.8, 10),
        (9, 55, 101.8, 103.0, 101.7, 102.9, 10),
        (10, 0, 102.9, 104.0, 102.5, 103.5, 10),
    ]
    daily = daily_bars([100.0] * 30)          # last_close = 100.0 ... see below
    daily[-1]["c"] = 100.0
    # Make the prior close 98.00 so the gap is a clean number: the module uses
    # the last COMPLETED close as the prior close.
    daily[-1]["c"] = 98.0
    client = FakeClient(daily={"G": daily}, five={"G": intraday_bars(rows)})
    snap = mc.symbol_snapshot(client, "G")
    assert snap["last_close"] == 98.0
    assert snap["intraday_high"] == 104.0 and snap["intraday_low"] == 99.5
    assert snap["orb_high"] == 103.0 and snap["orb_low"] == 99.5, (snap["orb_high"], snap["orb_low"])
    assert snap["gap_pct"] == round((100.0 / 98.0 - 1) * 100, 2) == 2.04, snap["gap_pct"]


@test
def test_orb_withheld_until_window_has_enough_bars():
    """A 5-minute range is NOT an opening range: with <3 window bars orb is None."""
    rows = [(9, 30, 100.0, 101.0, 99.5, 100.5, 10),
            (9, 35, 100.5, 102.0, 100.0, 101.5, 10)]
    client = FakeClient(daily={"O": daily_bars([100.0] * 30)},
                        five={"O": intraday_bars(rows)})
    snap = mc.symbol_snapshot(client, "O")
    assert snap["orb_high"] is None and snap["orb_low"] is None
    assert snap["intraday_high"] == 102.0 and snap["intraday_low"] == 99.5
    assert snap["gap_pct"] is not None


# --------------------------------------------------------------------------- #
# Snapshot contract / degradation
# --------------------------------------------------------------------------- #
@test
def test_snapshot_shape_and_text():
    client = FakeClient(daily={"META": daily_bars([100.0 + i for i in range(260)])},
                        prices={"META": 359.0})
    snap = mc.symbol_snapshot(client, "meta")   # lowercase in, uppercase out
    assert set(snap) == set(mc.SNAP_KEYS), set(snap) ^ set(mc.SNAP_KEYS)
    assert snap["symbol"] == "META"
    assert snap["text"].startswith("META 359.00 (")
    assert "ATR" in snap["text"] and "RSI" in snap["text"] and "200d SMA" in snap["text"]
    assert len(snap["text"]) <= 300


@test
def test_snapshot_never_raises_and_is_all_none_on_failure():
    client = FakeClient(boom=True)
    snap = mc.symbol_snapshot(client, "BOOM")
    assert set(snap) == set(mc.SNAP_KEYS)
    assert all(snap[k] is None for k in mc.SNAP_KEYS if k not in ("symbol", "text"))
    assert snap["text"] == "BOOM · no data"

    empty = FakeClient()   # symbol with 'bars': null
    snap2 = mc.symbol_snapshot(empty, "NOPE")
    assert all(snap2[k] is None for k in mc.SNAP_KEYS if k not in ("symbol", "text"))


@test
def test_snapshot_cache_ttl_and_isolation():
    client = FakeClient(daily={"C": daily_bars([100.0] * 30)})
    mc.symbol_snapshot(client, "C")
    n = len(client.calls)
    assert n > 0
    mc.symbol_snapshot(client, "C")           # served from cache
    assert len(client.calls) == n, "second call should not hit the data host"

    # Mutating the returned dict must not poison the cached copy.
    snap = mc.symbol_snapshot(client, "C")
    snap["last"] = 12345.0
    snap.pop("text")
    again = mc.symbol_snapshot(client, "C")
    assert again["last"] != 12345.0 and "text" in again

    mc.clear_cache()
    mc.symbol_snapshot(client, "C")
    assert len(client.calls) > n, "after clear_cache() it must refetch"


# --------------------------------------------------------------------------- #
# Regime label rules — all four labels, on synthetic inputs
# --------------------------------------------------------------------------- #
def regime_client(spy_closes, vixy=None, xlf=None):
    daily = {"SPY": daily_bars(spy_closes)}
    if vixy is not None:
        daily["VIXY"] = daily_bars(vixy)
    if xlf is not None:
        daily["XLF"] = daily_bars(xlf)
    return FakeClient(daily=daily, prices={"SPY": spy_closes[-1]})


@test
def test_regime_label_trend_up():
    mc._http_json = no_network                     # force the VIXY proxy path
    client = regime_client([100.0 + i * 0.5 for i in range(300)],
                           vixy=[15.0] * 20, xlf=[50.0 + i * 0.2 for i in range(300)])
    reg = mc.regime(client)
    assert reg["label"] == "trend_up", reg
    assert reg["spy_trend"] == "above_20_50"
    assert reg["vix"] == 15.0 and reg["vix_source"] == "vixy"
    assert reg["breadth_pct"] == 100.0 and reg["breadth_above_200_pct"] == 100.0
    assert "XLF" in reg["sectors"] and reg["sectors"]["XLF"]["rs_5d_vs_spy"] is not None
    assert len(reg["text"]) <= 200 and reg["text"].startswith("TREND_UP")


@test
def test_regime_label_trend_down():
    mc._http_json = no_network
    client = regime_client([300.0 - i * 0.5 for i in range(300)], vixy=[15.0] * 20)
    reg = mc.regime(client)
    assert reg["label"] == "trend_down", reg
    assert reg["spy_trend"] == "below_20_50"
    assert reg["breadth_pct"] is None          # no sector data at all -> absent


@test
def test_regime_label_chop():
    """Flat SPY: last == sma20 == sma50 -> 'mixed' -> chop (not a trend)."""
    mc._http_json = no_network
    client = regime_client([200.0] * 300, vixy=[15.0] * 20)
    reg = mc.regime(client)
    assert reg["label"] == "chop", reg
    assert reg["spy_trend"] == "mixed"
    assert reg["text"].startswith("CHOP · SPY 200.00")


@test
def test_regime_label_vol_spike_by_level_and_by_5d_rise():
    mc._http_json = no_network
    hot = regime_client([200.0] * 300, vixy=[35.0] * 20)      # VIX >= 28
    assert mc.regime(hot)["label"] == "vol_spike"
    mc.clear_cache()

    jumped = regime_client([200.0] * 300, vixy=[12.0] * 15 + [15.0] * 5)  # +25% 5d
    reg = mc.regime(jumped)
    assert reg["vix_chg_5d_pct"] == 25.0
    assert reg["label"] == "vol_spike", reg


@test
def test_regime_vol_spike_wins_over_structure_and_cboe_preferred():
    """A spike beats a textbook uptrend; CBOE is used when it answers."""
    mc._http_json = lambda url, headers, timeout=20: {"data": {"current_price": 17.3}}
    client = regime_client([100.0 + i * 0.5 for i in range(300)], vixy=[15.0] * 20)
    reg = mc.regime(client)
    assert reg["vix"] == 17.3 and reg["vix_source"] == "cboe"
    assert reg["label"] == "trend_up"          # 17.3 is not a spike

    mc.clear_cache()
    mc._http_json = lambda url, headers, timeout=20: {"data": {"current_price": 31.5}}
    reg2 = mc.regime(client)
    assert reg2["vix_source"] == "cboe" and reg2["label"] == "vol_spike"


@test
def test_regime_never_raises_and_fails_to_chop():
    client = FakeClient(boom=True)
    reg = mc.regime(client)
    assert set(reg) == set(mc.REGIME_KEYS), set(reg) ^ set(mc.REGIME_KEYS)
    assert reg["label"] == "chop"
    assert reg["spy_last"] is None and reg["breadth_pct"] is None
    assert len(reg["text"]) <= 200


@test
def test_regime_atr_percentile_bounds():
    mc._http_json = no_network
    client = regime_client([100.0 + i * 0.5 for i in range(300)], vixy=[15.0] * 20)
    reg = mc.regime(client)
    assert 0.0 <= reg["spy_atr_pctile_1y"] <= 100.0
    assert 0.0 < reg["spy_atr_pct"] < 10.0     # a plausible single-digit ATR%


# --------------------------------------------------------------------------- #
# Earnings
# --------------------------------------------------------------------------- #
@test
def test_earnings_within_maps_rows_and_times():
    today = datetime.now(ET).date()
    days = []
    d = today
    while len(days) < 4:                       # first 4 trading days ahead
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    payload = {
        days[0]: [{"symbol": "META", "time": "time-after-hours", "epsForecast": "$6.12"},
                  {"symbol": "SPY", "time": "time-pre-market", "epsForecast": None}],
        days[2]: [{"symbol": "NVDA", "time": "time-not-supplied", "epsForecast": "--"},
                  {"symbol": "AAPL", "time": "time-pre-market", "epsForecast": "$1.35"}],
    }
    mc._nasdaq_day = lambda day: payload.get(day, [])
    out = mc.earnings_within(None, ["META", "NVDA", "AAPL", "SPY", "ZZZZ"], 10)

    assert out["META"] == {"date": days[0].isoformat(), "in_days": 0,
                           "time": "after_close", "eps_forecast": "$6.12"}
    assert out["AAPL"]["time"] == "before_open" and out["AAPL"]["eps_forecast"] == "$1.35"
    assert out["NVDA"]["time"] == "unknown" and out["NVDA"]["eps_forecast"] is None
    # An ETF is simply absent from the calendar -> None, not an error.
    assert out["SPY"] is not None                # SPY IS a row in this fixture...
    assert out["ZZZZ"] is None                   # ...but an unknown symbol is not
    assert set(out) == {"META", "NVDA", "AAPL", "SPY", "ZZZZ"}


@test
def test_earnings_fails_open_and_skips_weekends():
    mc._nasdaq_day = lambda day: (_ for _ in ()).throw(RuntimeError("nasdaq down"))
    out = mc.earnings_within(None, ["META", "SPY"], 7)
    assert out == {"META": None, "SPY": None}

    fetched: list[date] = []

    def spy_day(day):
        fetched.append(day)
        return []

    mc._nasdaq_day = spy_day
    assert mc.earnings_within(None, ["META"], 7) == {"META": None}
    assert fetched and all(d.weekday() < 5 for d in fetched), fetched
    assert mc.earnings_within(None, [], 5) == {}


# --------------------------------------------------------------------------- #
# context_for_llm
# --------------------------------------------------------------------------- #
@test
def test_context_for_llm_shape_and_degradation():
    mc._http_json = no_network
    spy = [100.0 + i * 0.5 for i in range(300)]
    client = FakeClient(
        daily={"SPY": daily_bars(spy), "META": daily_bars([200.0] * 260),
               "XLF": daily_bars([50.0] * 260)},
        prices={"SPY": spy[-1], "META": 250.0},
    )
    mc._nasdaq_day = lambda day: [{"symbol": "META", "time": "time-after-hours",
                                   "epsForecast": "$1.00"}]
    ctx = mc.context_for_llm(client, ["META", "BOOM"])

    assert set(ctx) == {"regime", "symbols", "earnings", "text"}
    assert "sectors" not in ctx["regime"]            # kept compact for the prompt
    assert set(ctx["symbols"]) == {"META", "BOOM"}
    assert ctx["symbols"]["META"]["last"] == 250.0
    assert ctx["symbols"]["BOOM"]["last"] is None    # never omitted silently
    assert ctx["earnings"]["META"] is not None
    assert "earnings<=5d: META in 0d (after_close)" in ctx["text"]
    assert "{'sectors'}" not in ctx["text"]

    mc.clear_cache()
    ctx2 = mc.context_for_llm(client, ["META"], with_earnings=False)
    assert ctx2["earnings"] == {}


@test
def test_context_for_llm_never_raises_with_dead_client():
    mc._http_json = no_network
    ctx = mc.context_for_llm(FakeClient(boom=True), ["META", "SPY"])
    assert set(ctx) == {"regime", "symbols", "earnings", "text"}
    assert ctx["regime"]["label"] == "chop"
    assert all(ctx["symbols"][s]["last"] is None for s in ("META", "SPY"))


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    real_http_json = mc._http_json
    real_nasdaq_day = mc._nasdaq_day
    failed = 0
    for fn in TESTS:
        mc.clear_cache()
        mc._http_json = no_network          # default: no test may touch the network
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:  # a crash is a failure, not a stack trace dump
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"PASS  {fn.__name__}")
    mc._http_json = real_http_json
    mc._nasdaq_day = real_nasdaq_day
    total = len(TESTS)
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
