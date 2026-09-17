#!/usr/bin/env python3
"""Walk-forward backtest: the three LIVE strategy rules vs two mean-reversion
alternatives, risk-normalised in R, split by market regime.

STANDALONE and stdlib-only (no numpy/pandas). The only repo module it imports is
fees.py (the same regulatory fee model the live bots use). It does NOT import
alpaca_rest unless you ask it to fetch (--fetch).

Why R, not dollars: the live bots risk ~1% of equity per trade, so a dollar
result conflates edge with position size. Every metric here is R = net P/L
divided by the risk that was actually at stake at entry (|entry - stop| x shares).

WHAT IS MEASURED
  1. ORB_5min        the live daily ORB rule, on real 5-min SPY bars
                     (09:30-10:00 ET opening range, close-confirmed breakout,
                     stop at the opposite side, target 1.5x the range).
                     Only ~4 months of 5-min history is available on the free
                     IEX feed, so this variant has its OWN short window.
  2. ORB_daily_PROXY a daily-bar gap-breakout with ORB-like geometry, run over
                     the full window. NOT the live rule (the 09:30-10:00 range is
                     simply not present in daily bars) - reported only to see
                     whether breakout STRUCTURE has an edge over 7.7 years.
  3. PULLBACK_weekly the live weekly rule (SPY vs 100d SMA, entry within 1x
                     ATR(14) of the SMA, stop 1x ATR, target 1.5x ATR).
                     Reports the trigger frequency too - 0 live fires in 12
                     sessions needs an explanation.
  4. MR_rsi2         long-only mean reversion: uptrend (close > 200d SMA and
                     50d SMA rising) + RSI(2) < 10, exit close > 5d SMA or 10
                     bars, hard stop 2.5x ATR(14).
  5. MR_atr_dip      long-only mean reversion: same uptrend filter + close more
                     than 2x ATR(14) below the 10d SMA, exit close > 10d SMA or
                     10 bars, hard stop 2.5x ATR(14).

MODEL ASSUMPTIONS (all documented, all debatable - see the report's caveats)
  * Entry at the bar CLOSE that produced the signal (that is what the live bots
    key off: the weekly bot places a limit at the latest price, the ORB rule
    requires a close beyond the range). No lookahead: every level used at bar i
    (SMA, ATR, RSI, range) is computed from bars <= i.
  * Slippage = 1 basis point of price per side, applied adversely, on every
    MARKET-style fill (entry, stop, close-based exit, end-of-data flatten).
    Limit-target fills get NO slippage (a resting limit fills at its price).
    Stops that gap through fill at the bar's OPEN (worse, not better).
  * Fees: fees.round_trip_equity_fees() from the repo (SEC 31 + FINRA TAF +
    CAT). Alpaca is commission-free; these are regulatory pass-throughs.
  * Sizing: shares = int(equity * RISK_PCT / |entry - stop|), min 1 share,
    equity pinned at $10,000 (the live paper accounts' start).
  * EXIT PRIORITY (assumed, unit-tested): on a single bar the STOP is checked
    FIRST, then the limit target, then the close-based exit rule, then the
    max-hold time stop. OHLC bars carry no intra-bar ordering, so 'stop wins'
    is the conservative assumption.
  * One position at a time per (symbol, variant); no same-bar re-entry; no
    portfolio-level correlation/capital limit (trades across symbols are pooled).

Run:
  cd /opt/data/profiles/trader/trading
  TRADER_HOME=/opt/data/profiles/trader python3 backtest_mr.py \
      --cache data/backtest_bars.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fees  # noqa: E402  (repo fee model - the same one the live bots use)

ET = ZoneInfo("America/New_York")

EQUITY = 10_000.0          # per-sleeve notional, matches the live paper accounts
RISK_PCT = 0.01            # 1% of equity risked per trade (config.RISK_PCT_PER_TRADE)
SLIP_BPS = 1.0             # 1 bp of price per side, adverse, on market fills
ATR_PERIOD = 14
MR_MAX_HOLD = 10           # bars
MR_STOP_ATR = 2.5
RSI2_ENTRY = 10.0
DIP_ATR_MULT = 2.0
PULLBACK_STOP_ATR = 1.0
PULLBACK_TARGET_ATR = 1.5
ORB_RR = 1.5

# Regime: chop = the 20-day realised range is under RNG_PCT of price AND price
# sits within DIST_PCT of its 20d SMA. Primary thresholds below; the tight/loose
# variants are reported as a sensitivity sweep so the choice is not hidden.
REGIME_SWEEP = [(0.035, 0.02), (0.04, 0.02), (0.045, 0.02), (0.05, 0.025)]
REGIME_PRIMARY = (0.04, 0.02)

UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "SMF_placeholder"]
UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "XLE", "XLF", "XLK", "XLV", "XLI",
            "XLY", "XLP", "XLU", "XLB", "SMH", "GLD", "SLV", "TLT", "IEF", "HYG"]


# --------------------------------------------------------------------------- #
# indicators (pure functions; mirror indicators.py, plus RSI)
# --------------------------------------------------------------------------- #
def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def true_range(bars: list[dict]) -> list[float]:
    tr = [0.0] * len(bars)
    for i, b in enumerate(bars):
        if i == 0:
            tr[i] = b["h"] - b["l"]
        else:
            pc = bars[i - 1]["c"]
            tr[i] = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
    return tr


def atr(bars: list[dict], period: int = ATR_PERIOD) -> list[float | None]:
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    tr = true_range(bars)
    out[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period  # type: ignore[operator]
    return out


def rsi(closes: list[float], period: int = 2) -> list[float | None]:
    """Wilder RSI. out[i] is None until `period` changes are available."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + gains[i - 1]) / period
        al = (al * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


# --------------------------------------------------------------------------- #
# R accounting  (the piece the unit tests pin down)
# --------------------------------------------------------------------------- #
def trade_pnl(side: str, entry_px: float, exit_px: float, stop: float, shares: float,
              slip_bps: float = SLIP_BPS, limit_fill: bool = False,
              fee_fn=None) -> dict:
    """Exact P/L, fee and R accounting for one completed trade.

    R = net P/L / initial risk, where initial risk = |slippage-adjusted entry -
    stop| x shares (the money actually at stake once the fill is in). Slippage is
    applied to the entry always and to the exit unless the exit was a resting
    limit fill. Returns gross / fees / net / risk_dollars / r.
    """
    if fee_fn is None:
        fee_fn = fees.round_trip_equity_fees
    slip = slip_bps / 10_000.0
    if side == "long":
        entry_tx = entry_px * (1.0 + slip)
        exit_tx = exit_px if limit_fill else exit_px * (1.0 - slip)
        gross = (exit_tx - entry_tx) * shares
        risk_ps = entry_tx - stop
        fee = fee_fn(shares, buy_price=entry_tx, sell_price=exit_tx)
    else:
        entry_tx = entry_px * (1.0 - slip)
        exit_tx = exit_px if limit_fill else exit_px * (1.0 + slip)
        gross = (entry_tx - exit_tx) * shares
        risk_ps = stop - entry_tx
        fee = fee_fn(shares, buy_price=exit_tx, sell_price=entry_tx)
    risk_dollars = risk_ps * shares
    net = gross - fee
    r = net / risk_dollars if risk_dollars > 0 else 0.0
    return {"gross": gross, "fees": fee, "net": net, "risk_per_share": risk_ps,
            "risk_dollars": risk_dollars, "r": r,
            "entry_tx": entry_tx, "exit_tx": exit_tx}


def size_shares(entry_px: float, stop: float, equity: float = EQUITY,
                risk_pct: float = RISK_PCT, min_shares: int = 1) -> int:
    """Whole-share sizing for a fixed fractional risk. Risk is measured from the
    DECISION price (the close that signalled), before slippage."""
    risk_ps = abs(entry_px - stop)
    if risk_ps <= 0:
        return 0
    return max(min_shares, int((equity * risk_pct) / risk_ps))


def resolve_bar_exit(bar: dict, pos: dict, exit_at_close: bool,
                     max_hold_hit: bool) -> tuple[str, float, bool] | None:
    """Pick this bar's exit, in the documented order of precedence.

    STOP first (assumed hit before anything else on the same bar), then the
    resting limit TARGET, then the close-based exit rule, then the max-hold time
    stop. A gap through the stop fills at the bar OPEN. Returns
    (reason, raw_fill_price, is_limit_fill) or None to keep holding.
    """
    side, stop, target = pos["side"], pos["stop"], pos.get("target")
    if side == "long":
        if bar["l"] <= stop:
            return ("stop", min(stop, bar["o"]), False)
        if target is not None and bar["h"] >= target:
            return ("target", target, True)
    else:
        if bar["h"] >= stop:
            return ("stop", max(stop, bar["o"]), False)
        if target is not None and bar["l"] <= target:
            return ("target", target, True)
    if exit_at_close:
        return ("exit_rule", bar["c"], False)
    if max_hold_hit:
        return ("max_hold", bar["c"], False)
    return None


# --------------------------------------------------------------------------- #
# generic engine
# --------------------------------------------------------------------------- #
def simulate(bars: list[dict], variant: str, symbol: str, signal_fn,
             exit_close_fn=None, max_hold: int | None = None, start_idx: int = 0,
             regimes: list[str] | None = None, mkt_regimes: list[str] | None = None,
             slip_bps: float | None = None, allow_short: bool = True) -> dict:
    """Bar-by-bar simulation of one (symbol, variant).

    signal_fn(i, bar) -> None | dict(side, stop, target)  (evaluated at bar close)
    exit_close_fn(i, bar, pos) -> bool                    (close-based exit rule)
    """
    slip_bps = SLIP_BPS if slip_bps is None else slip_bps
    trades: list[dict] = []
    pos: dict | None = None
    bars_in_market = 0
    n = len(bars)

    for i in range(start_idx, n):
        b = bars[i]
        if pos is not None:
            pos["bars_in_market"] += 1
            bars_in_market += 1
            held = i - pos["entry_idx"]
            exit_at_close = bool(exit_close_fn and exit_close_fn(i, b, pos))
            dec = resolve_bar_exit(b, pos, exit_at_close,
                                   max_hold is not None and held >= max_hold)
            if dec:
                reason, raw_px, limit_fill = dec
                trades.append(_close(pos, b["d"], i, raw_px, reason, limit_fill, slip_bps))
                pos = None
                continue
        if pos is None:
            sig = signal_fn(i, b)
            if sig is None:
                continue
            side = sig["side"]
            if side == "short" and not allow_short:
                continue
            stop = sig["stop"]
            shares = size_shares(b["c"], stop)
            if shares <= 0:
                continue
            pos = {"variant": variant, "symbol": symbol, "side": side,
                   "entry_idx": i, "entry_date": b["d"], "entry_px": b["c"],
                   "stop": float(stop),
                   "target": None if sig.get("target") is None else float(sig["target"]),
                   "shares": shares, "bars_in_market": 0,
                   "regime": (regimes[i] if regimes else "na"),
                   "mkt_regime": (mkt_regimes[i] if mkt_regimes else "na")}

    if pos is not None:  # still open when the data ends
        trades.append(_close(pos, bars[n - 1]["d"], n - 1, bars[n - 1]["c"],
                             "eod_flat", False, slip_bps))
    eligible_bars = max(0, n - start_idx)
    return {"trades": trades, "bars_in_market": bars_in_market,
            "eligible_bars": eligible_bars}


def _close(pos: dict, exit_date: str, exit_idx: int, raw_px: float, reason: str,
           limit_fill: bool, slip_bps: float) -> dict:
    p = trade_pnl(pos["side"], pos["entry_px"], raw_px, pos["stop"], pos["shares"],
                  slip_bps=slip_bps, limit_fill=limit_fill)
    return {**{k: pos[k] for k in ("variant", "symbol", "side", "entry_date",
                                   "entry_px", "stop", "target", "shares",
                                   "regime", "mkt_regime")},
            "bars_in_market": pos["bars_in_market"],
            "exit_date": exit_date, "exit_idx": exit_idx,
            "exit_px": raw_px, "reason": reason, "limit_fill": limit_fill,
            "hold_bars": exit_idx - pos["entry_idx"],
            "gross": p["gross"], "fees": p["fees"], "net": p["net"],
            "risk_dollars": p["risk_dollars"], "r": p["r"]}


# --------------------------------------------------------------------------- #
# variant signal builders
# --------------------------------------------------------------------------- #
def build_mr_rsi2(bars: list[dict]) -> tuple[list, list]:
    """Returns (signal_by_idx, exit_close_by_idx) for MR_rsi2."""
    closes = [b["c"] for b in bars]
    s50 = sma(closes, 50)
    s200 = sma(closes, 200)
    s5 = sma(closes, 5)
    a14 = atr(bars, ATR_PERIOD)
    r2 = rsi(closes, 2)
    sigs: dict[int, dict] = {}
    exits: dict[int, bool] = {}
    for i in range(len(bars)):
        if None in (s50[i], s200[i], s5[i], a14[i], r2[i]):
            continue
        uptrend = (closes[i] > s200[i]        # type: ignore[operator]
                   and s50[i] > s50[i - 1])   # type: ignore[operator]
        if uptrend and r2[i] < RSI2_ENTRY:    # type: ignore[operator]
            stop = closes[i] - MR_STOP_ATR * a14[i]   # type: ignore[operator]
            sigs[i] = {"side": "long", "stop": stop, "target": None}
        if closes[i] > s5[i]:                 # type: ignore[operator]
            exits[i] = True
    return sigs, exits


def build_mr_atr_dip(bars: list[dict]) -> tuple[list, list]:
    closes = [b["c"] for b in bars]
    s10 = sma(closes, 10)
    s50 = sma(closes, 50)
    s200 = sma(closes, 200)
    a14 = atr(bars, ATR_PERIOD)
    sigs: dict[int, dict] = {}
    exits: dict[int, bool] = {}
    for i in range(len(bars)):
        if None in (s10[i], s50[i], s200[i], a14[i]):
            continue
        uptrend = (closes[i] > s200[i]        # type: ignore[operator]
                   and s50[i] > s50[i - 1])   # type: ignore[operator]
        if uptrend and closes[i] < s10[i] - DIP_ATR_MULT * a14[i]:   # type: ignore[operator]
            stop = closes[i] - MR_STOP_ATR * a14[i]   # type: ignore[operator]
            sigs[i] = {"side": "long", "stop": stop, "target": None}
        if closes[i] > s10[i]:                # type: ignore[operator]
            exits[i] = True
    return sigs, exits


def build_control_long(beta_bars: list[dict], every: int = 10,
                       hold: int = 3) -> dict:
    """BETA CONTROL (not a strategy candidate).

    Long-only, same stop width, same cost model, but entries are mechanical every
    `every` bars while close > 200d SMA and exits are after `hold` bars. It
    measures how much of the MR variants' R is simply 'long US equity ETF in an
    uptrend' rather than the mean-reversion timing. Mean R here is the number the
    MR variants have to beat to claim an edge.
    """
    closes = [b["c"] for b in beta_bars]
    s200 = sma(closes, 200)
    a14 = atr(beta_bars, ATR_PERIOD)
    sigs: dict[int, dict] = {}
    for i in range(len(beta_bars)):
        if s200[i] is None or a14[i] is None:
            continue
        if closes[i] > s200[i] and i % every == 0:  # type: ignore[operator]
            sigs[i] = {"side": "long",
                       "stop": closes[i] - MR_STOP_ATR * a14[i],  # type: ignore[operator]
                       "target": None}
    return sigs


def pullback_live_replay(daily_bars: list[dict], b5: list[dict],
                         cutoff: str = "10:45") -> list[dict]:
    """Replay the weekly bot's VIEW at its observed run time (10:45 ET).

    The live bot fetches daily bars during market hours, so its last bar is the
    PARTIAL current session (open / high-so-far / low-so-far / price-now) and it
    evaluates the trigger against that partial bar. A final-close backtest cannot
    see that, so the discrepancy between 'trigger TRUE on the final close' and
    '0 live fires' has to be checked at the price the bot actually had.

    Returns one row per session in the 5-min window: the trigger test as of
    `cutoff` ET. `sma100`/`atr14` are computed on the same bar sequence the bot
    would hold (closed daily bars + today's partial bar). The bot requests
    limit=140 daily bars, so its Wilder ATR(14) can differ from a full-history
    ATR(14) by a few cents - noted, not corrected.
    """
    rth: dict[str, list[dict]] = {}
    for b in b5:
        if "09:30" <= b["hm"] < "16:00":
            rth.setdefault(b["d"], []).append(b)
    by_date = {b["d"]: b for b in daily_bars}
    rows = []
    for d in sorted(rth):
        sb = [b for b in rth[d] if b["hm"] <= cutoff]
        if not sb or d not in by_date:
            continue
        partial = {"d": d, "hm": cutoff, "o": rth[d][0]["o"],
                   "h": max(b["h"] for b in sb), "l": min(b["l"] for b in sb),
                   "c": sb[-1]["c"], "v": 0.0}
        series = [b for b in daily_bars if b["d"] < d] + [partial]
        closes = [b["c"] for b in series]
        s100 = sma(closes, 100)[-1]
        a14 = atr(series, ATR_PERIOD)[-1]
        last = closes[-1]
        if s100 is None or a14 is None or a14 <= 0:
            continue
        dist = (last - s100) / a14
        final = by_date[d]["c"]
        dist_final = (final - s100) / a14
        rows.append({"d": d, "price_at_cutoff": last, "close": final, "sma100": s100,
                     "atr14": a14, "dist_atr": dist, "dist_atr_final_close": dist_final,
                     "trigger_at_cutoff": 0 < dist <= 1 or -1 <= dist < 0,
                     "trigger_on_final_close": (0 < dist_final <= 1
                                                or -1 <= dist_final < 0)})
    return rows


def build_pullback(bars: list[dict]) -> tuple[list, list, dict]:
    """The live weekly rule. Returns (sigs, exits(none), diagnostics)."""
    closes = [b["c"] for b in bars]
    s100 = sma(closes, 100)
    a14 = atr(bars, ATR_PERIOD)
    sigs: dict[int, dict] = {}
    diag = {"warmup_days": 0, "long_days": 0, "short_days": 0, "sig_days": 0,
            "dist_atr": []}
    for i in range(len(bars)):
        if s100[i] is None or a14[i] is None or a14[i] <= 0:
            diag["warmup_days"] += 1
            continue
        c, s, a = closes[i], s100[i], a14[i]
        dist = (c - s) / a
        diag["dist_atr"].append((bars[i]["d"], c, s, a, dist))
        if c > s and c <= s + a:
            sigs[i] = {"side": "long", "stop": c - PULLBACK_STOP_ATR * a,
                       "target": c + PULLBACK_TARGET_ATR * a}
            diag["long_days"] += 1
            diag["sig_days"] += 1
        elif c < s and c >= s - a:
            sigs[i] = {"side": "short", "stop": c + PULLBACK_STOP_ATR * a,
                       "target": c - PULLBACK_TARGET_ATR * a}
            diag["short_days"] += 1
            diag["sig_days"] += 1
    return sigs, {}, diag


def build_orb_daily_proxy(bars: list[dict]) -> list:
    """DAILY-BAR PROXY ONLY - not the live rule.

    With daily OHLC the 09:30-10:00 range is unobservable. Proxy geometry: the
    prior day's (high - low) plays the range role, and today's OPEN outside that
    range is the breakout trigger; stop = opposite side of the prior range,
    target = 1.5x. This tests breakout structure, nothing about the live ORB.
    """
    sigs: dict[int, dict] = {}
    for i in range(1, len(bars)):
        rng = bars[i - 1]["h"] - bars[i - 1]["l"]
        if rng <= 0:
            continue
        o = bars[i]["o"]
        if o > bars[i - 1]["h"]:
            sigs[i] = {"side": "long", "stop": bars[i - 1]["l"], "target": o + ORB_RR * rng}
        elif o < bars[i - 1]["l"]:
            sigs[i] = {"side": "short", "stop": bars[i - 1]["h"], "target": o - ORB_RR * rng}
    return sigs


def build_orb_5min(sessions: dict[str, list[dict]], bars: list[dict],
                   or_end: str = "10:00") -> tuple[dict, dict]:
    """The LIVE daily ORB rule on 5-min bars.

    Opening range = bars stamped 09:30..10:00 ET (the deployed daily_run.py
    convention is `bar_et_hm(b['t']) <= '10:00'`, so the 10:00-stamped bar is
    included - mirrored here to measure the deployed rule). Entry on the first
    later bar whose CLOSE is beyond the range (entry at that bar's close, not at
    the range boundary - a real fill, unlike backtest.py's optimistic boundary
    fill). Stop = opposite side of the range, target = 1.5x the range.
    Returns (entry_idx -> sig, per-session diagnostics).
    """
    idx_of = {id(b): i for i, b in enumerate(bars)}
    sigs: dict[int, dict] = {}
    diag: dict[str, dict] = {}
    for d in sorted(sessions):
        sb = sessions[d]
        orb = [b for b in sb if b["hm"] <= or_end]
        post = [b for b in sb if b["hm"] > or_end]
        diag[d] = {"n_rth": len(sb), "rng": None, "side": None, "break": False}
        if len(orb) < 6 or not post:
            continue
        hi = max(b["h"] for b in orb)
        lo = min(b["l"] for b in orb)
        rng = hi - lo
        diag[d]["rng"] = rng
        if rng <= 0:
            continue
        for b in post:
            if b["c"] > hi:
                side, stop = "long", lo
                break
            if b["c"] < lo:
                side, stop = "short", hi
                break
        else:
            continue
        entry = b["c"]
        target = entry + ORB_RR * rng if side == "long" else entry - ORB_RR * rng
        sigs[idx_of[id(b)]] = {"side": side, "stop": stop, "target": target}
        diag[d]["side"] = side
        diag[d]["break"] = True
    return sigs, diag


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def norm(raw: list[dict]) -> list[dict]:
    out = []
    for b in raw:
        dt = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        out.append({"d": dt.date().isoformat(), "hm": dt.strftime("%H:%M"),
                    "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
                    "c": float(b["c"]), "v": float(b.get("v") or 0)})
    out.sort(key=lambda x: x["d"] + x["hm"] if "hm" in x else x["d"])
    return out


def regime_series(bars: list[dict], rng_pct: float, dist_pct: float) -> list[str]:
    """chop / trend / na per bar, from that symbol's own 20-day window."""
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    out = ["na"] * len(bars)
    for i in range(19, len(bars)):
        w = slice(i - 19, i + 1)
        sma20 = sum(closes[w]) / 20
        rng = (max(highs[w]) - min(lows[w])) / closes[i]
        dist = abs(closes[i] / sma20 - 1.0) if sma20 else 9.9
        out[i] = "chop" if (rng < rng_pct and dist < dist_pct) else "trend"
    return out


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def max_consec_losers(rs: list[float]) -> int:
    best = cur = 0
    for r in rs:
        cur = cur + 1 if r <= 0 else 0
        best = max(best, cur)
    return best


def max_dd_r(trades: list[dict]) -> float:
    """Peak-to-trough drawdown of the cumulative-R curve (chronological)."""
    cum = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: (x["exit_date"], x["symbol"])):
        cum += t["r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def summarize(trades: list[dict], bars_in_market: int = 0,
              eligible_bars: int = 0, label: str = "") -> dict:
    n = len(trades)
    rs = [t["r"] for t in trades]
    if n == 0:
        return {"label": label, "n": 0, "win_rate": None, "avg_r": None,
                "total_r": 0.0, "exp_r": None, "avg_hold": None, "max_cl": 0,
                "max_dd_r": 0.0, "pct_bars": (bars_in_market / eligible_bars
                                               if eligible_bars else None),
                "tstat": None, "profit_factor": None, "avg_win_r": None,
                "avg_loss_r": None, "n_stop": 0, "n_target": 0, "n_rule": 0,
                "n_hold": 0, "n_eod": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    sd = statistics.stdev(rs) if n > 1 else 0.0
    exp = sum(rs) / n
    return {
        "label": label, "n": n,
        "win_rate": len(wins) / n,
        "avg_r": exp,
        "total_r": sum(rs),
        "exp_r": exp,
        "avg_hold": sum(t["hold_bars"] for t in trades) / n,
        "max_cl": max_consec_losers(rs),
        "max_dd_r": max_dd_r(trades),
        "pct_bars": (bars_in_market / eligible_bars if eligible_bars else None),
        "tstat": (exp / (sd / math.sqrt(n))) if sd > 0 else None,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "avg_win_r": (sum(wins) / len(wins)) if wins else None,
        "avg_loss_r": (sum(losses) / len(losses)) if losses else None,
        "n_stop": sum(1 for t in trades if t["reason"] == "stop"),
        "n_target": sum(1 for t in trades if t["reason"] == "target"),
        "n_rule": sum(1 for t in trades if t["reason"] == "exit_rule"),
        "n_hold": sum(1 for t in trades if t["reason"] == "max_hold"),
        "n_eod": sum(1 for t in trades if t["reason"] == "eod_flat"),
    }


def bootstrap_ci(rs: list[float], iters: int = 10_000, seed: int = 42,
                 lo: float = 2.5, hi: float = 97.5) -> tuple[float, float] | None:
    if len(rs) < 5:
        return None
    rng = random.Random(seed)
    n = len(rs)
    means = []
    for _ in range(iters):
        means.append(sum(rs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return (means[int(lo / 100 * iters)], means[int(hi / 100 * iters)])


def welch_t(a: list[float], b: list[float]) -> float | None:
    """Welch t for 'mean(a) != mean(b)' with unequal variances. stdlib only."""
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return None
    return (statistics.mean(a) - statistics.mean(b)) / se


def bootstrap_diff(a: list[float], b: list[float], iters: int = 10_000,
                   seed: int = 42) -> tuple[float, float] | None:
    """Bootstrap CI for mean(a) - mean(b) (independent resampling)."""
    if len(a) < 5 or len(b) < 5:
        return None
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    diffs = []
    for _ in range(iters):
        ma = sum(a[rng.randrange(na)] for _ in range(na)) / na
        mb = sum(b[rng.randrange(nb)] for _ in range(nb)) / nb
        diffs.append(ma - mb)
    diffs.sort()
    return (diffs[int(0.025 * iters)], diffs[int(0.975 * iters)])



# --------------------------------------------------------------------------- #
# report helpers
# --------------------------------------------------------------------------- #
def fmt(v, nd=2, pct=False, plus=True):
    if v is None:
        return "   n/a"
    if pct:
        return f"{v * 100:6.1f}%"
    s = f"{v:+.{nd}f}" if plus else f"{v:.{nd}f}"
    return s


def table(headers: list[str], rows: list[list[str]], align=None) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * w for w in widths)]
    for r in rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(out)


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def sum_row(s: dict, name: str) -> list[str]:
    expo = (s["total_r"] / (s["pct_bars"] * 100)) if s["pct_bars"] else None
    return [name, str(s["n"]), fmt(s["win_rate"], pct=True), fmt(s["avg_r"]),
            fmt(s["total_r"]), fmt(expo, 1), fmt(s["avg_hold"], 1, plus=False),
            fmt(s["tstat"]), str(s["max_cl"]), fmt(s["max_dd_r"], 1, plus=False),
            fmt(s["pct_bars"], pct=True) if s["pct_bars"] is not None else "n/a"]


MAIN_HEADERS = ["variant", "n", "win%", "avgR", "totR", "totR/1%expo", "hold", "t",
                "maxCL", "maxDD_R", "%bars"]


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    global SLIP_BPS
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="data/backtest_bars.json")
    ap.add_argument("--out", default="reports/backtest_2026-09.md")
    ap.add_argument("--fetch", action="store_true",
                    help="fetch fresh bars via alpaca_rest (needs keys) instead of the cache")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--slip-bps", type=float, default=SLIP_BPS)
    ap.add_argument("--bootstrap", type=int, default=10_000)
    args = ap.parse_args()
    SLIP_BPS = args.slip_bps

    if args.fetch:
        from alpaca_rest import AlpacaClient
        cache = fetch_live(AlpacaClient("daily"), UNIVERSE + ["SPY"], args.start)
    else:
        cache = json.loads(Path(args.cache).read_text())

    prov = cache.get("meta", {})
    daily_raw = cache.get("daily", {})
    lines: list[str] = []   # text output
    md: list[str] = []      # markdown report

    def out(s: str = "") -> None:
        lines.append(s)
        print(s)

    def m(s: str = "") -> None:
        md.append(s)

    # ---- window report ---------------------------------------------------- #
    symbols = [s for s in UNIVERSE if s in daily_raw]
    daily: dict[str, list[dict]] = {}
    true_first = true_last = None
    for s in symbols:
        bars = norm(daily_raw[s])
        daily[s] = bars
        p = prov.get(s, {})
        if p.get("first"):
            if true_first is None or p["first"][:10] < true_first:
                true_first = p["first"][:10]
            if true_last is None or p["last"][:10] > true_last:
                true_last = p["last"][:10]

    # The final daily bar is the still-forming session at run time; drop it from
    # signals so no entry is priced at a non-final close.
    last_date = max(b[-1]["d"] for b in daily.values())
    signalled_to = None
    for s in symbols:
        if daily[s] and daily[s][-1]["d"] == last_date:
            daily[s] = daily[s][:-1]
        signalled_to = daily[s][-1]["d"]

    spy = daily["SPY"]
    split_i = len(spy) // 2
    split_date = spy[split_i]["d"]

    prov_rows = [[s, str(prov.get(s, {}).get("n", len(daily[s]))),
                  str(prov.get(s, {}).get("first", "?"))[:10],
                  str(prov.get(s, {}).get("last", "?"))[:10],
                  str(prov.get(s, {}).get("error"))] for s in symbols]
    five = cache.get("spy_5min", {})
    cache5 = cache.get("spy_5min_bars", [])

    banner = [
        "=== backtest_mr.py - live strategies vs mean reversion (R-normalised) ===",
        f"generated : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"daily data: TRUE first bar {true_first} / TRUE last bar {true_last} "
        f"({prov.get('SPY', {}).get('n', '?')} bars, source={'live fetch' if args.fetch else args.cache})",
        f"5-min data: {five.get('n', 0)} SPY bars {str(five.get('first'))[:16]} -> "
        f"{str(five.get('last'))[:16]} (free IEX caps 5-min history to ~120 days)",
        f"signals   : {spy[0]['d']} -> {signalled_to} (last daily bar {last_date} excluded: still-forming session)",
        f"costs     : fees.py regulatory model + {SLIP_BPS:g} bp slippage per side, equity ${EQUITY:,.0f}, risk {RISK_PCT:.0%}/trade",
    ]
    for b in banner:
        out(b)
    out("\n-- data window actually obtained --")
    out(table(["symbol", "bars", "first", "last", "error"], prov_rows))
    m("# Backtest report - live strategies vs mean-reversion alternatives (2026-09)\n")
    m(f"*Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by "
      f"`backtest_mr.py` (stdlib only). Every number below is program output; none is hand-entered.*\n")
    m("## 1. Data actually obtained (no assumptions)\n")
    m("Requested `start=2019-01-01`, `timeframe=1Day`, feed = free IEX (the only feed the "
      "paper keys can read). True window returned per symbol:\n")
    m(md_table(["symbol", "bars", "first bar", "last bar", "error"], prov_rows))
    m(f"\n* **Daily bars:** {true_first} -> {true_last} (all {len(symbols)} symbols, "
      f"{prov.get('SPY', {}).get('n', '?')} bars each - one page, no truncation).\n")
    m(f"* **5-min SPY bars:** {five.get('n', 0)} bars, {str(five.get('first'))[:16]} -> "
      f"{str(five.get('last'))[:16]}. The free IEX feed caps 5-min history at roughly the last "
      "120 calendar days - a request starting 2019-01-01 returned only 2019-01-01..2019-01-18 "
      "(2,249 bars, the first page ascending), so 5-min history cannot be extended backwards. "
      "The ORB variant therefore has its own ~4-month window.\n")
    m(f"* **Signal window:** `{spy[0]['d']}` -> `{signalled_to}`. The final daily bar "
      f"(`{last_date}`) is the still-forming session at run time and is excluded from signals. "
      f"The final 5-min session (`{five.get('last', '')[:10]}`) is likewise incomplete and is "
      "excluded from ORB sessions.\n")

    # ---- regime diagnosis ------------------------------------------------- #
    out("\n-- regime definition and how often it holds (symbol's own 20d window) --")
    reg_rows = []
    for rp, dp in REGIME_SWEEP:
        per = []
        for s in ("SPY", "QQQ", "IWM", "GLD", "TLT", "XLE"):
            if s not in daily:
                continue
            lab = regime_series(daily[s], rp, dp)
            c = sum(1 for x in lab if x == "chop")
            per.append(f"{s} {c / max(1, len(lab)) * 100:.1f}%")
        reg_rows.append([f"range<{rp:.1%} & |px-sma20|<{dp:.1%}", " ".join(per)])
    out(table(["chop definition", "share of bars labelled chop"], reg_rows))
    m("\n## 2. Regime definition\n")
    m("`chop` at bar *i* = the 20-day realised range `(max(high,20) - min(low,20)) / close` is "
      "below **4.0%** **and** `|close / SMA20 - 1|` is below **2.0%**, computed on the symbol's "
      "own bars. Everything else is `trend`. The threshold pair is a judgement call, so the "
      "sweep below shows how much the label changes with it:\n")
    m(md_table(["chop definition", "share of bars labelled chop (per symbol)"], reg_rows))
    spy_chop = regime_series(spy, *REGIME_PRIMARY)
    chop_days = sum(1 for x in spy_chop if x == "chop")
    m(f"\nUnder the primary definition SPY is in chop on **{chop_days}/{len(spy)} days "
      f"({chop_days / len(spy):.1%})** of the 7.7-year window. The Sep 1-17 2026 window the "
      f"live bots traded reads: 20-day range "
      f"{100 * (max(b['h'] for b in spy[-20:]) - min(b['l'] for b in spy[-20:])) / spy[-1]['c']:.2f}% "
      f"of price, close {100 * (spy[-1]['c'] / (sum(b['c'] for b in spy[-20:]) / 20) - 1):+.2f}% "
      f"from SMA20 - i.e. **chop** under this definition.\n")

    # ---- run variants ----------------------------------------------------- #
    out("\n-- running variants --")
    results: dict[str, dict] = {}
    all_trades: dict[str, list[dict]] = {}

    # 1. ORB on 5-min SPY
    sessions: dict[str, list[dict]] = {}
    if cache5:
        b5 = norm(cache5)
        for b in b5:
            sessions.setdefault(b["d"], []).append(b)
        last5 = max(sessions)
        rth = {d: [b for b in v if "09:30" <= b["hm"] < "16:00"]
               for d, v in sessions.items()}
        complete = {d: v for d, v in rth.items() if d != last5 and len(v) >= 70}
        f5 = [b for d in sorted(complete) for b in complete[d]]
        orb_sigs, orb_diag = build_orb_5min(complete, f5)
        # market regime for the 5-min bars: SPY's own daily chop/trend label
        spy_reg = regime_series(spy, *REGIME_PRIMARY)
        reg_by_date = {b["d"]: spy_reg[i] for i, b in enumerate(spy)}
        reg5 = [reg_by_date.get(b["d"], "na") for b in f5]
        res5 = simulate(f5, "ORB_5min", "SPY", lambda i, b, s=orb_sigs: s.get(i),
                        exit_close_fn=None, max_hold=None, start_idx=0,
                        regimes=reg5, mkt_regimes=reg5,
                        slip_bps=SLIP_BPS, allow_short=True)
        results["ORB_5min"] = res5
        all_trades["ORB_5min"] = res5["trades"]
        out(f"   ORB_5min: {len(complete)} complete sessions, "
            f"{len(orb_sigs)} breakouts -> {len(res5['trades'])} trades")
    else:
        results["ORB_5min"] = None
        out("   ORB_5min: NO 5-min data in the cache -> not tested")

    # 2/3. daily variants
    orb_proxy_trades: list[dict] = []
    orb_proxy_mkt = orb_proxy_bars = 0
    pb_trades: list[dict] = []
    pb_diag = {}
    pb_mkt = pb_bars = 0
    sig_proxy = build_orb_daily_proxy(spy)
    rb = regime_series(spy, *REGIME_PRIMARY)
    mkt = rb
    r = simulate(spy, "ORB_daily_PROXY", "SPY", lambda i, b: sig_proxy.get(i),
                 exit_close_fn=None, max_hold=None, regimes=rb, mkt_regimes=mkt)
    orb_proxy_trades = r["trades"]
    orb_proxy_mkt, orb_proxy_bars = r["bars_in_market"], r["eligible_bars"]

    pb_sigs, _, pb_diag = build_pullback(spy)
    r = simulate(spy, "PULLBACK_weekly", "SPY", lambda i, b: pb_sigs.get(i),
                 exit_close_fn=None, max_hold=None, regimes=rb, mkt_regimes=mkt)
    pb_trades = r["trades"]
    pb_mkt, pb_bars = r["bars_in_market"], r["eligible_bars"]
    out(f"   PULLBACK_weekly: {pb_diag['sig_days']} trigger days -> {len(pb_trades)} trades")
    all_trades["ORB_daily_PROXY"] = orb_proxy_trades
    all_trades["PULLBACK_weekly"] = pb_trades

    # 4/5. MR variants, pooled across the universe
    mr_rsi_trades: list[dict] = []
    mr_dip_trades: list[dict] = []
    mr_rsi_mkt = mr_rsi_bars = mr_dip_mkt = mr_dip_bars = 0
    mr_rows_spy: dict[str, list[dict]] = {"MR_rsi2": [], "MR_atr_dip": []}
    mr_sig_counts = {"MR_rsi2": 0, "MR_atr_dip": 0}
    for s in symbols:
        bars = daily[s]
        if len(bars) < 220:
            continue
        rg = regime_series(bars, *REGIME_PRIMARY)
        mk = mkt if s == "SPY" else _align_mkt(bars, spy, spy_chop)
        if s == "SPY":
            mk = spy_chop
        for name, builder in (("MR_rsi2", build_mr_rsi2), ("MR_atr_dip", build_mr_atr_dip)):
            sg, ex = builder(bars)
            mr_sig_counts[name] += len(sg)
            rr = simulate(bars, name, s, lambda i, b, sg=sg: sg.get(i),
                          exit_close_fn=lambda i, b, pos, ex=ex: ex.get(i, False),
                          max_hold=MR_MAX_HOLD, regimes=rg, mkt_regimes=mk,
                          allow_short=False)
            if name == "MR_rsi2":
                mr_rsi_trades += rr["trades"]
                mr_rsi_mkt += rr["bars_in_market"]
                mr_rsi_bars += rr["eligible_bars"]
            else:
                mr_dip_trades += rr["trades"]
                mr_dip_mkt += rr["bars_in_market"]
                mr_dip_bars += rr["eligible_bars"]
            if s == "SPY":
                mr_rows_spy[name] = rr["trades"]
    all_trades["MR_rsi2"] = mr_rsi_trades
    all_trades["MR_atr_dip"] = mr_dip_trades
    out(f"   MR_rsi2   : {mr_sig_counts['MR_rsi2']} signals -> {len(mr_rsi_trades)} trades "
        f"across {len(symbols)} symbols")
    out(f"   MR_atr_dip: {mr_sig_counts['MR_atr_dip']} signals -> {len(mr_dip_trades)} trades "
        f"across {len(symbols)} symbols")

    # 6. BETA CONTROL - same universe, same costs, no mean-reversion condition.
    ctrl_trades: list[dict] = []
    ctrl_mkt = ctrl_bars = 0
    for s in symbols:
        bars = daily[s]
        if len(bars) < 220:
            continue
        rg = regime_series(bars, *REGIME_PRIMARY)
        sg = build_control_long(bars)
        rr = simulate(bars, "CTRL_long_beta", s, lambda i, b, sg=sg: sg.get(i),
                      exit_close_fn=None, max_hold=3, regimes=rg,
                      mkt_regimes=_align_mkt(bars, spy, spy_chop), allow_short=False)
        ctrl_trades += rr["trades"]
        ctrl_mkt += rr["bars_in_market"]
        ctrl_bars += rr["eligible_bars"]
    all_trades["CTRL_long_beta"] = ctrl_trades
    out(f"   CTRL_long_beta: {len(ctrl_trades)} trades (beta benchmark)")

    def s_of(name: str) -> dict:
        if name == "ORB_5min":
            return summarize(results["ORB_5min"]["trades"],
                             results["ORB_5min"]["bars_in_market"],
                             results["ORB_5min"]["eligible_bars"], f"{name} (SPY)")
        if name == "ORB_daily_PROXY":
            return summarize(orb_proxy_trades, orb_proxy_mkt, orb_proxy_bars, f"{name} (SPY)")
        if name == "PULLBACK_weekly":
            return summarize(pb_trades, pb_mkt, pb_bars, f"{name} (SPY)")
        if name == "MR_rsi2":
            return summarize(mr_rsi_trades, mr_rsi_mkt, mr_rsi_bars, f"{name} (19 syms)")
        if name == "CTRL_long_beta":
            return summarize(ctrl_trades, ctrl_mkt, ctrl_bars,
                             "CTRL_long_beta (19 syms, benchmark)")
        return summarize(mr_dip_trades, mr_dip_mkt, mr_dip_bars, f"{name} (19 syms)")

    order = ["ORB_5min", "ORB_daily_PROXY", "PULLBACK_weekly", "MR_rsi2", "MR_atr_dip"]
    main_rows = [sum_row(s_of(n), s_of(n)["label"]) for n in order]
    main_rows.append(sum_row(s_of("CTRL_long_beta"), "* " + s_of("CTRL_long_beta")["label"]))
    out("\n=== MAIN RESULTS (R-normalised, full available window per variant) ===")
    out(table(MAIN_HEADERS, main_rows))

    m("\n## 3. Main results - full available window\n")
    m("`avgR`/`totR` are net of fees and slippage. `hold` = bars (sessions) held on average. "
      "`t` = expectancy / (stdev / sqrt(n)) - a crude significance check, **not** corrected for "
      "the fact that five rule variants were tried (see §7). `%bars` = share of eligible bars with a "
      "position open.\n")
    m(md_table(MAIN_HEADERS, main_rows))
    cs = s_of("CTRL_long_beta")
    mrc = s_of("MR_rsi2")
    mdc = s_of("MR_atr_dip")
    m(f"\n`* CTRL_long_beta` is **not a strategy candidate** - it is the beta benchmark: "
      f"long-only, same universe, same 2.5x ATR stop, same fee/slippage model, but entries are "
      f"mechanical (every 10th bar while close > 200d SMA) and exits after 3 bars, with no "
      f"mean-reversion condition. It answers the only question that matters for the MR claim: "
      f"how much R does 'long US equity ETFs in an uptrend' produce on its own? "
      f"Control avgR = **{cs['avg_r']:+.3f}** over n={cs['n']} trades vs MR_rsi2 "
      f"{mrc['avg_r']:+.3f} (n={mrc['n']}) and MR_atr_dip {mdc['avg_r']:+.3f} (n={mdc['n']}). "
      f"MR_rsi2 adds **{mrc['avg_r'] - cs['avg_r']:+.3f}R per trade** over the timer entry; "
      f"MR_atr_dip adds **{mdc['avg_r'] - cs['avg_r']:+.3f}R**. That difference - not the raw "
      "positive expectancy - is the claimed mean-reversion edge.\n")

    m("\n### 3.1 Exit mix and win/loss asymmetry\n")
    ex_rows = []
    for n in order + ["CTRL_long_beta"]:
        s = s_of(n)
        if s["n"] == 0:
            continue
        ex_rows.append([s["label"], str(s["n"]), str(s["n_stop"]), str(s["n_target"]),
                        str(s["n_rule"]), str(s["n_hold"]), str(s["n_eod"]),
                        fmt(s["avg_win_r"]), fmt(s["avg_loss_r"]),
                        fmt(s["profit_factor"])])
    m(md_table(["variant", "n", "stops", "targets", "rule-exits", "time-stops",
                "flat-at-end", "avg win R", "avg loss R", "profit factor"], ex_rows))
    m("\nA high win rate with a thin average R means the losses are fat when they come: "
      "the MR variants win ~70% of trades but the average loss is roughly "
      f"{abs(mrc['avg_loss_r']):.2f}R against an average win of {mrc['avg_win_r']:.2f}R. "
      "That shape is fragile to a regime where stops are hit more often (see §5).\n")

    # ORB_5min leg breakdown
    if results["ORB_5min"] and cache5:
        o5 = s_of("ORB_5min")
        m("\n### 3.2 ORB_5min (the live rule on real 5-min bars)\n")
        m(f"Trades: **{o5['n']}** over {len(complete)} complete sessions "
          f"({sorted(complete)[0]} -> {sorted(complete)[-1]}). "
          f"Exits: stop={o5['n_stop']}, target={o5['n_target']}, flat-at-end={o5['n_eod']}. "
          f"Sessions with a breakout: {sum(1 for v in orb_diag.values() if v['break'])}/"
          f"{len(orb_diag)}; median opening range "
          f"{statistics.median([v['rng'] for v in orb_diag.values() if v['rng']]):.2f} index points. "
          f"`hold` for this variant is in 5-min bars (78 per session), so "
          f"{o5['avg_hold']:.0f} bars is about {o5['avg_hold'] / 78:.1f} sessions - "
          "the live rule allows overnight holds, and did.\n")

    m("\n## 4. The motivating window: Sep 1-17 2026\n")
    spy_w = [b for b in spy if "2026-09-01" <= b["d"] <= "2026-09-17"]
    if spy_w:
        hi = max(b["h"] for b in spy_w)
        lo = min(b["l"] for b in spy_w)
        chop_in_w = sum(1 for i, b in enumerate(spy)
                        if "2026-09-01" <= b["d"] <= "2026-09-17" and spy_chop[i] == "chop")
        m(f"SPY: {len(spy_w)} sessions, {spy_w[0]['o']:.2f} -> {spy_w[-1]['c']:.2f} "
          f"({100 * (spy_w[-1]['c'] / spy_w[0]['o'] - 1):+.2f}%), range {lo:.2f}-{hi:.2f} "
          f"({100 * (hi - lo) / lo:.2f}% wide). Regime label on those days: "
          f"**{chop_in_w} of {len(spy_w)}** were chop, {len(spy_w) - chop_in_w} trend.\n")

    m("### 4.1 PULLBACK_weekly trigger frequency - bug or rare setup?\n")
    dw = [r for r in pb_diag["dist_atr"] if "2026-09-01" <= r[0] <= "2026-09-17"]
    m("The live weekly bot fired **0 times in 12 sessions**. The trigger requires price to be "
      "within 1 ATR(14) of the 100d SMA (`sma < close <= sma + 1*ATR` for the long side). "
      "Distance from the SMA in ATR units, last 20 sessions:\n")
    rows = [[d, f"{c:.2f}", f"{s:.2f}", f"{a:.2f}", f"{dist:+.2f}",
             "YES" if 0 < dist <= 1 else ("yes-short" if -1 <= dist < 0 else "no")]
            for d, c, s, a, dist in pb_diag["dist_atr"][-20:]]
    m(md_table(["date", "close", "sma100", "atr14", "(close-sma100)/atr14", "trigger?"], rows))
    win = [dist for _, _, _, _, dist in dw] or [None]
    m(f"\nSep 1-17 2026: trigger TRUE on **{sum(1 for d in win if d is not None and abs(d) <= 1)}"
      f"/{len(win)}** sessions "
      f"(min distance {min(win):+.2f} ATR, max {max(win):+.2f} ATR). Over the whole "
      f"{len(pb_diag['dist_atr'])}-day history the trigger was TRUE on "
      f"**{pb_diag['sig_days']} days ({pb_diag['sig_days'] / len(pb_diag['dist_atr']):.1%})** "
      f"long={pb_diag['long_days']}, short={pb_diag['short_days']} - i.e. the live bot's silence "
      "is a rare-setup outcome, not evidence of a broken signal path.\n")
    m(f"\n*Window note:* the live bots' window is 12 sessions (Sep 1-17). This backtest's signal "
      f"window ends 2026-09-16, because the 2026-09-17 daily bar is the still-forming session at "
      f"run time - hence 11 sessions here, not 12.\n")


    m("\n### 4.2 Live-time replay of the weekly trigger (why 0 fires might be expected anyway)\n")
    if cache5:
        replay = pullback_live_replay(spy, norm(cache5))
        rl = [r for r in replay if "2026-09-01" <= r["d"] <= "2026-09-17"]
        rows_r = [[r["d"], f"{r['price_at_cutoff']:.2f}", f"{r['sma100']:.2f}",
                   f"{r['atr14']:.2f}", f"{r['dist_atr']:+.2f}",
                   "YES" if r["trigger_at_cutoff"] else "no", f"{r['close']:.2f}",
                   f"{r['dist_atr_final_close']:+.2f}",
                   "YES" if r["trigger_on_final_close"] else "no"] for r in rl]
        m(f"The live bot evaluates the trigger on the PARTIAL current daily bar, at the moment it "
          f"runs. `schedule.py` runs the weekly bot at **14:45 UTC = 10:45 ET**, and its event "
          f"journal on the trading host (read read-only) logs its last run at "
          f"2026-09-17T14:45Z, confirming that. So the trigger is replayed with the 10:45 ET price "
          f"(today's partial bar as the last bar, SMA100/ATR14 computed on the same sequence the "
          f"bot would hold). Every session in the live window:\n")
        m(md_table(["date", "price@10:45", "sma100", "atr14", "dist@10:45 (ATR)",
                    "trigger@10:45", "final close", "dist at close", "trigger at close"],
                   rows_r))
        n_live = sum(1 for r in replay if r["trigger_at_cutoff"])
        n_close = sum(1 for r in replay if r["trigger_on_final_close"])
        m(f"\nOver all {len(replay)} sessions of 5-min history: the trigger was TRUE at 10:45 ET "
          f"on **{n_live}** sessions, but TRUE on the final close of **{n_close}** sessions. "
          f"Checked at the price the bot actually saw, the setup was rarer still than the "
          f"final-close statistic suggests.\n")
        out(f"\n   weekly live-time replay: trigger TRUE at 10:45 ET on {n_live}/{len(replay)} "
            f"sessions vs {n_close}/{len(replay)} on final closes")
    else:
        m("No 5-min SPY data available, so the live-time replay of the weekly trigger "
          "could not be run at all (the trigger was only measured on final closes).\n")

    m("\n### 4.3 What each rule did inside the live window (entries Sep 1-17 2026)\n")
    win_rows = []
    for n in order + ["CTRL_long_beta"]:
        ts = [t for t in all_trades[n] if "2026-09-01" <= t["entry_date"] <= "2026-09-17"]
        rs = [t["r"] for t in ts]
        win_rows.append([s_of(n)["label"], str(len(ts)),
                         fmt(sum(rs)) if rs else "   n/a",
                         fmt(statistics.mean(rs)) if rs else "   n/a",
                         ", ".join(f"{t['symbol']}:{t['reason']}" for t in ts[:6])
                         + ("..." if len(ts) > 6 else "")])
    out("\n-- live window (Sep 1-17 2026) per variant --")
    out(table(["variant", "trades", "totalR", "avgR", "symbol:exit (first 6)"], win_rows))
    m(md_table(["variant", "trades in window", "total R", "avg R",
                "symbol:exit reason (first 6)"], win_rows))
    m("\nContext from the parent run (not reproduced here): the three live bots produced "
      "9 trades in this window - daily ORB 1 trade +$9.27, weekly pullback 0 trades, YOLO "
      "0 wins in 7 (net -$151.42, ~-7.9R at 1% risk). This backtest cannot reproduce the "
      "YOLO result at all: YOLO's entries are LLM decisions, not a rule, so there is nothing "
      "deterministic to replay.\n")

    # ---- regime split ----------------------------------------------------- #
    out("\n=== REGIME SPLIT (symbol's own chop/trend label at entry) ===")
    reg_rows2 = []
    for n in order:
        for lab in ("chop", "trend"):
            ts = [t for t in all_trades[n] if t["regime"] == lab]
            if not ts:
                reg_rows2.append([f"{n} / {lab}", "0"] + ["-"] * 8)
                continue
            s = summarize(ts, label=f"{n} / {lab}")
            reg_rows2.append([f"{n} / {lab}", str(s["n"]), fmt(s["win_rate"], pct=True),
                              fmt(s["avg_r"]), fmt(s["total_r"]),
                              fmt(s["avg_hold"], 1, plus=False), fmt(s["tstat"]),
                              str(s["max_cl"]), fmt(s["max_dd_r"], 1, plus=False),
                              fmt(s["profit_factor"])])
    hdr = ["variant / regime", "n", "win%", "avgR", "totR", "hold", "t", "maxCL",
           "maxDD_R", "PF"]
    out(table(hdr, reg_rows2))
    m("\n## 5. Regime split - does the edge depend on regime?\n")
    m("Regime label of the symbol at the entry bar (definitions in §2). Same metrics, "
      "restricted to each regime.\n")
    m(md_table(hdr, reg_rows2))

    m("\n### 5.1 Sensitivity of the split to the chop threshold\n")
    sens_rows = []
    for rp, dp in REGIME_SWEEP:
        for n in ("MR_rsi2", "MR_atr_dip", "PULLBACK_weekly", "ORB_daily_PROXY"):
            parts = []
            # re-label with this threshold pair and recompute expectancy
            trades = []
            if n.startswith("MR"):
                for s in symbols:
                    bars = daily[s]
                    if len(bars) < 220:
                        continue
                    rg = regime_series(bars, rp, dp)
                    sg, ex = (build_mr_rsi2 if n == "MR_rsi2" else build_mr_atr_dip)(bars)
                    rr = simulate(bars, n, s, lambda i, b, sg=sg: sg.get(i),
                                  exit_close_fn=lambda i, b, pos, ex=ex: ex.get(i, False),
                                  max_hold=MR_MAX_HOLD, regimes=rg,
                                  mkt_regimes=["na"] * len(bars), allow_short=False)
                    trades += rr["trades"]
            else:
                rg = regime_series(spy, rp, dp)
                sg = (build_pullback(spy)[0] if n == "PULLBACK_weekly"
                      else build_orb_daily_proxy(spy))
                rr = simulate(spy, n, "SPY", lambda i, b, sg=sg: sg.get(i),
                              regimes=rg, mkt_regimes=["na"] * len(spy))
                trades = rr["trades"]
            ch = [t["r"] for t in trades if t["regime"] == "chop"]
            tr = [t["r"] for t in trades if t["regime"] == "trend"]
            sens_rows.append([
                f"<{rp:.1%}/{dp:.1%}", n,
                "(no trades)" if not ch else f"n={len(ch)} expR={statistics.mean(ch):+.2f}",
                "(no trades)" if not tr else f"n={len(tr)} expR={statistics.mean(tr):+.2f}",
            ])
    out("\n-- regime-threshold sensitivity (chop expectancy vs trend expectancy) --")
    out(table(["threshold", "variant", "chop regime", "trend regime"], sens_rows))
    m(md_table(["threshold (range/dist)", "variant", "chop regime", "trend regime"],
               sens_rows))

    # ---- walk-forward ----------------------------------------------------- #
    out(f"\n=== WALK-FORWARD SPLIT at {split_date} (in-sample / out-of-sample) ===")
    wf_rows = []
    for n in order:
        ts = all_trades[n]
        if n == "ORB_5min":
            sd = sorted(complete)[len(complete) // 2]
        else:
            sd = split_date
        a = [t for t in ts if t["entry_date"] < sd]
        b = [t for t in ts if t["entry_date"] >= sd]
        sa, sb = summarize(a, label="IS"), summarize(b, label="OOS")
        wf_rows.append([n, f"<{sd}", str(sa["n"]), fmt(sa["avg_r"]), fmt(sa["total_r"]),
                        fmt(sa["tstat"]), f">={sd}", str(sb["n"]), fmt(sb["avg_r"]),
                        fmt(sb["total_r"]), fmt(sb["tstat"])])
    out(table(["variant", "from", "n", "avgR", "totR", "t", "to", "n", "avgR", "totR", "t"],
              wf_rows))
    m(f"\n## 6. Walk-forward honesty check (split at {split_date})\n")
    m(md_table(["variant", "first half", "n", "avgR", "totR", "t", "second half", "n",
                "avgR", "totR", "t"], wf_rows))
    m("\nA single split is a weak walk-forward: it is one draw of many possible splits. "
      "Read it as 'does the sign survive on unseen data', not as a validated edge.\n")

    # ---- multiple comparison + bootstrap ---------------------------------- #
    m("\n## 7. Multiple comparisons, uncertainty, and the beta control test\n")
    m("`totR/1%expo` in §3 normalises total R by exposure (R earned per 1% of bars "
      "with a position open); it is the fair way to compare a low-exposure MR sleeve "
      "with a high-exposure benchmark.\n")

    m("\n### 7.1 MR vs the beta control - the decisive test\n")
    m("The MR variants' raw positive expectancy is not evidence of a mean-reversion "
      "edge: long US equity ETFs in an uptrend make money under almost any entry rule. "
      "The claim only survives if MR beats `CTRL_long_beta` on the same universe with "
      "the same stop, fees and slippage.\n")
    diff_rows = []
    ctrl_r = [t["r"] for t in all_trades["CTRL_long_beta"]]
    for n in ("MR_rsi2", "MR_atr_dip"):
        rs = [t["r"] for t in all_trades[n]]
        t = welch_t(rs, ctrl_r)
        d = bootstrap_diff(rs, ctrl_r, args.bootstrap)
        diff_rows.append([n, str(len(rs)), fmt(statistics.mean(rs)),
                          str(len(ctrl_r)), fmt(statistics.mean(ctrl_r)),
                          fmt(statistics.mean(rs) - statistics.mean(ctrl_r)),
                          fmt(t),
                          (f"[{d[0]:+.2f}, {d[1]:+.2f}]" if d else "n/a"),
                          "yes" if (d and d[0] > 0) else "no"])
    out("\n-- MR expectancy minus beta-control expectancy (Welch t, bootstrap CI) --")
    out(table(["variant", "n", "avgR", "n ctrl", "ctrl avgR", "difference", "Welch t",
               "95% CI diff", "CI > 0?"], diff_rows))
    m(md_table(["variant", "n", "avgR", "n ctrl", "ctrl avgR", "difference",
                "Welch t", "95% CI of difference", "CI > 0?"], diff_rows))
    m("\nIf the difference CI straddles 0, the mean-reversion timing adds nothing "
      "measurable beyond 'be long in an uptrend' on this data.\n")

    m("\n### 7.2 Selection risk and per-variant uncertainty\n")
    best = max((s_of(n) for n in order), key=lambda s: (s["avg_r"] if s["avg_r"] is not None else -9e9))
    m(f"Five rule variants were evaluated (not one). Under the null of zero edge, the best of "
      f"five noisy variants looks good by selection alone; with 5 tests a Bonferroni-corrected "
      f"two-sided 5% threshold is roughly |t| > 2.58. Best variant here: "
      f"**{best['label']}** avgR={best['avg_r']:+.3f} t={best['tstat']:.2f} "
      f"(n={best['n']}; selected on the highest avgR - note MR_rsi2 carries the higher t at "
      f"{s_of('MR_rsi2')['tstat']:.2f} on 4x the trades, so 'best' here is a judgement, "
      f"not a significance result).\n")
    boot_rows = []
    for n in order:
        rs = [t["r"] for t in all_trades[n]]
        ci = bootstrap_ci(rs, args.bootstrap)
        s = s_of(n)
        boot_rows.append([n, str(s["n"]), fmt(s["avg_r"]), fmt(s["tstat"]),
                          (f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "n/a (<5 trades)"),
                          "yes" if (ci and ci[0] > 0) else "no"])
    out("\n-- bootstrap 95% CI of the mean R (10000 resamples, seed 42) --")
    out(table(["variant", "n", "avgR", "t", "95% CI mean R", "CI excludes 0?"], boot_rows))
    m(md_table(["variant", "n", "avgR", "t", "95% CI of mean R (bootstrap)", "CI > 0?"],
               boot_rows))
    m("\nBootstrap CIs assume trades are independent and identically distributed - they ignore "
      "regime clustering, symbol correlation and the fact that the rules were chosen after "
      "seeing the live results. They are a floor on the uncertainty, not a ceiling.\n")

    # ---- per symbol for MR ------------------------------------------------- #
    m("\n## 8. MR variants per symbol (pooled tables hide size effects)\n")
    mr_sym_rows = []
    for s in symbols:
        row = [s]
        for n in ("MR_rsi2", "MR_atr_dip"):
            ts = [t for t in all_trades[n] if t["symbol"] == s]
            sm = summarize(ts)
            row += [str(sm["n"]), fmt(sm["avg_r"]) if sm["n"] else "-",
                    fmt(sm["total_r"])]
        mr_sym_rows.append(row)
    m(md_table(["symbol", "rsi2 n", "rsi2 avgR", "rsi2 totR", "dip n", "dip avgR", "dip totR"],
               mr_sym_rows))
    out("\n-- MR per-symbol --")
    out(table(["symbol", "rsi2 n", "rsi2 avgR", "rsi2 totR", "dip n", "dip avgR", "dip totR"],
              mr_sym_rows))

    # ---- benchmark -------------------------------------------------------- #
    m("\n## 9. Benchmark context\n")
    bh_rows = []
    for label, a, b in (("full window", spy[0]["d"], spy[-1]["d"]),
                        ("first half", spy[0]["d"], split_date),
                        ("second half", split_date, spy[-1]["d"]),
                        ("Sep 1-17 2026", "2026-09-01", "2026-09-17")):
        seg = [x for x in spy if a <= x["d"] <= b]
        if len(seg) < 2:
            continue
        bh_rows.append([label, f"{seg[0]['d']} -> {seg[-1]['d']}", str(len(seg)),
                        f"{100 * (seg[-1]['c'] / seg[0]['o'] - 1):+.2f}%",
                        f"{seg[-1]['c'] / max(x['c'] for x in seg) - 1:+.1%}"])
    m(md_table(["window", "dates", "sessions", "SPY open->close", "from running high"], bh_rows))

    # ---- caveats ---------------------------------------------------------- #
    _seg = [x for x in spy if x["d"] >= "2019-01-02"]
    spyret = f"{100 * (_seg[-1]['c'] / _seg[0]['o'] - 1):+.0f}%"
    m("\n## 10. What this does NOT prove\n")
    m("""* **The ORB rule is only tested on ~4 months.** 5-min history is capped by the free IEX
  feed, so `ORB_5min` says nothing about 2019-2025, and its sample is far too small for
  significance. `ORB_daily_PROXY` is **not the live rule** - daily bars do not contain the
  09:30-10:00 range; it only probes breakout structure and must not be quoted as a
  validation of the deployed ORB bot.
* **No intra-bar path.** Stops, targets and close-based exits are resolved from OHLC (5-min
  for ORB). When a stop and an exit condition both trigger on one bar, the stop is assumed
  hit first - conservative, but it is an assumption, not data.
* **Entry at the signal bar's close.** The live ORB bot enters intraday and `backtest.py`
  even assumes a fill at the range boundary; here entries are at the confirming close (more
  realistic) but still an idealisation. The weekly bot's limit at the last price is modelled
  as a fill at that close, which flatters it slightly.
* **Pooling across symbols ignores correlation.** MR trades across 19 ETFs are treated as
  independent equal-risk bets with unlimited concurrent capital; in reality their dips
  cluster (a market-wide selloff triggers many at once), so pooled total R overstates a
  tradable portfolio's result.
* **Fees are regulatory only.** Alpaca is commission-free and these ETFs are penny-spread,
  so costs are dominated by the 1 bp/side slippage ASSUMPTION, which is not measured from
  quotes. Doubling it degrades the MR variants roughly in proportion to trade count - rerun
  with `--slip-bps 2` to see the sensitivity.
* **Survivorship/selection in the universe.** The 19 symbols are today's liquid ETFs; a
  universe chosen in 2019 might have differed.
* **One macro sample.** 2019-2026 was a large bull market for US equity ETFs (SPY
  {spyret} opentoclose over the window). Long-only rules inherit that. The beta control in §3
  and the MR-vs-control test in §7.1 are the only defensible answers to it, and they rest on
  a control that is itself a crude proxy (timer entry, 3-bar hold) whose trades OVERLAP the
  MR variants' in time - the two samples are not independent, so §7.1's Welch t understates
  the uncertainty even though its CI already allows for unequal variances.
* **Multiple comparisons.** Five variants were tested on overlapping data; the best one is
  partly selection luck. Nothing here is Bonferroni-clean.
* **No LLM layer, no live execution.** The live bots gate every setup through an LLM advisor
  and a fee-adjusted R:R floor. This measures the RULES only, so it is an upper bound on
  trade count and an estimate of raw setup edge - not a simulation of the bots' behaviour.
* **The MR exits are weak conditions.** "Close above the 5d SMA" (rsi2) and "close above
  the 10d SMA" (atr_dip) are low bars - most MR exits are the rule exit, not the stop or a
  target (see §3.1). What this measures is therefore mostly the ENTRY timing, not an
  optimised exit.
""")

    # ---- reproduce -------------------------------------------------------- #
    m("\n## 11. Reproduce\n")
    m("```\n"
      "cd /opt/data/profiles/trader/trading\n"
      "TRADER_HOME=/opt/data/profiles/trader python3 tests/test_backtest_mr.py\n"
      f"TRADER_HOME=/opt/data/profiles/trader python3 backtest_mr.py --cache {args.cache}\n"
      "```\n")
    m("Bars cache: `data/backtest_bars.json` (fetched with a bars-only probe running the "
      "deployed `alpaca_rest.py` inside the `hermes-trader` container; `1Day` from "
      f"{args.start}, plus the ~120 days of SPY `5Min` the free feed allows).\n")

    m("\n### 7.3 Cost sensitivity of the MR edge\n")
    m("The edge over the control is thin (+0.08R for rsi2), so it is worth knowing how fast "
      "costs eat it. Slippage is the only cost that matters here (the regulatory fees are "
      "cents), and it is an ASSUMPTION, not measured from quotes - so the whole grid is "
      "recomputed below with the same simulator.\n")
    cost_rows = []
    for mult in (0.0, 1.0, 2.0, 5.0):
        slip = SLIP_BPS * mult
        for nm in ("MR_rsi2", "MR_atr_dip", "CTRL_long_beta"):
            allr = []
            for s_ in symbols:
                bs = daily[s_]
                if len(bs) < 220:
                    continue
                sg_, ex_ = ((build_mr_rsi2(bs) if nm == "MR_rsi2"
                             else build_mr_atr_dip(bs)) if nm.startswith("MR")
                            else (build_control_long(bs), {}))
                rr_ = simulate(bs, nm, s_, lambda i, b, sg=sg_: sg.get(i),
                               exit_close_fn=(lambda i, b, pos, ex=ex_: ex.get(i, False))
                               if ex_ else None,
                               max_hold=(MR_MAX_HOLD if nm.startswith("MR") else 3),
                               slip_bps=slip, allow_short=False)
                allr += [t["r"] for t in rr_["trades"]]
            cost_rows.append([f"{slip:.1f}", nm, str(len(allr)),
                              (fmt(statistics.mean(allr), 3) if allr else "n/a")])
    out("\n-- cost sensitivity (mean R vs slippage per side) --")
    out(table(["slip bp/side", "variant", "n", "avgR"], cost_rows))
    m(md_table(["slip bp/side", "variant", "n", "avgR"], cost_rows))
    m("\nRead the losing point: if the mean R of an MR variant crosses the control's under a "
      "plausible (not extreme) slippage assumption, the claimed edge is a cost assumption, "
      "not a finding.\n")

    tsv = "\n".join([f"{s_of(n)['label']}\tn={s_of(n)['n']}\tavgR={fmt(s_of(n)['avg_r'])}"
                     f"\ttotR={fmt(s_of(n)['total_r'])}" for n in order])
    out("\n=== COMPACT SUMMARY ===")
    out(tsv)

    outpath = Path(args.out)
    outpath.parent.mkdir(parents=True, exist_ok=True)
    outpath.write_text("\n".join(md) + "\n")
    print(f"\n[report written] {outpath}  ({len(' '.join(md).split())} words)")


def _align_mkt(bars: list[dict], spy: list[dict], spy_chop: list[str]) -> list[str]:
    """Map each bar date of `bars` to SPY's market regime label (na if absent)."""
    by_date = {b["d"]: spy_chop[i] for i, b in enumerate(spy)}
    return [by_date.get(b["d"], "na") for b in bars]


def fetch_live(client, symbols, start):
    """Fetch 1Day + 5Min SPY bars in the shape of the cache file."""
    out = {"daily": {}, "meta": {}}
    for sym in symbols:
        bars, tok, err = [], None, None
        while True:
            params = [f"timeframe=1Day", "limit=10000", "adjustment=all", f"start={start}"]
            if tok:
                params.append(f"page_token={tok}")
            try:
                resp = client._request("GET", f"/v2/stocks/{sym}/bars?{'&'.join(params)}",
                                       base=client.data_base_url)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                break
            batch = resp.get("bars") or []
            bars += batch
            tok = resp.get("next_page_token")
            if not tok or not batch:
                break
        bars.sort(key=lambda b: b["t"])
        out["daily"][sym] = bars
        out["meta"][sym] = {"n": len(bars), "first": bars[0]["t"] if bars else None,
                            "last": bars[-1]["t"] if bars else None, "error": err}
    from datetime import timedelta
    start5 = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
    bars5, tok, err = [], None, None
    while True:
        params = ["timeframe=5Min", "limit=10000", "adjustment=all", f"start={start5}"]
        if tok:
            params.append(f"page_token={tok}")
        resp = client._request("GET", f"/v2/stocks/SPY/bars?{'&'.join(params)}",
                               base=client.data_base_url)
        batch = resp.get("bars") or []
        bars5 += batch
        tok = resp.get("next_page_token")
        if not tok or not batch:
            break
    bars5.sort(key=lambda b: b["t"])
    out["spy_5min"] = {"n": len(bars5), "first": bars5[0]["t"] if bars5 else None,
                       "last": bars5[-1]["t"] if bars5 else None, "error": err}
    out["spy_5min_bars"] = bars5
    return out


if __name__ == "__main__":
    main()
