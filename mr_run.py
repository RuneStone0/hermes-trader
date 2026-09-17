"""Mean-reversion sleeve — buy short-term weakness inside a longer uptrend.

WHY THIS EXISTS (and why it lives on the daily account)
-------------------------------------------------------
Every strategy in this repo was a trend/breakout rule. Measured over the
Sep 1-17 2026 chop tape (SPY inside a 2.5% band) they produced 9 trades and no
edge, and a stdlib walk-forward backtest over 2019-2026
(`backtest_mr.py` → `reports/backtest_2026-09.md`) confirmed the ranking rather
than the hope:

    ORB_5min (the live daily rule)   n=57    avgR -0.01   t -0.09
    PULLBACK_weekly                  n=76    avgR +0.11   t +0.77
    MR_rsi2  <-- THIS SLEEVE         n=775   avgR +0.12   t +6.33
    CTRL_long_beta (hold ETFs up)    n=2434  avgR +0.04   t +3.65

The control row is the one that matters: long US equity ETFs in an uptrend earn
money under almost any entry rule, so a positive expectancy by itself proves
nothing. MR's edge is the DIFFERENCE — +0.078R per trade over that control
(Welch t +3.71, 95% CI [+0.04, +0.12], excluding zero), positive in both regimes
and both walk-forward halves, and it survives 2 bp/side slippage.

THE RULE (mirrored exactly from the backtest, deliberately)
-----------------------------------------------------------
  * long only, on liquid ETFs;
  * uptrend filter: close > SMA(200) AND SMA(50) rising;
  * entry when RSI(2) < 10 — i.e. buy a two-day dip in something that is
    structurally strong, instead of chasing something that has already run;
  * hard stop 2.5 x ATR(14);
  * exit on a close above SMA(5), or after 10 sessions (time stop).

Deviations from the backtest, all documented rather than hidden:
  1. Alpaca brackets REQUIRE a take-profit leg, and the backtest had none. The
     target here is a deliberately WIDE 4 x ATR outer cap that the rule exit
     normally beats to it — an outer bound, not a tuned parameter.
  2. Entries are only evaluated in the last 25 minutes of the session
     (`entry_window_et`): the tested signal is "the CLOSE is a dip", and running
     the check all day would enter on intraday wicks the backtest never saw. The
     forming bar's close is replaced with the live price, which is the closest a
     live bot can get to "evaluated at the close".
  3. Exits follow the same close-based basis, so an exit fills on the next tick
     (the rule is a close condition by construction). The stop is always live.
  4. SPY is excluded from the universe: the daily ORB owns SPY on this account,
     and two sleeves in one symbol makes attribution and bracket integrity
     impossible.

RISK SHAPE — read this before raising size
-----------------------------------------
~70% of trades win, but the average win is +0.37R against an average loss of
-0.57R: the losses are fat when they come, and the 2019-2026 sample is one long
bull market. That is why risk is 0.75% per trade with a 2-position cap
(correlated dips cluster) and why the drawdown governor sits above everything.

Usage: python3 mr_run.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import advisor
import config
import db
import guards
import market
import wording
from alpaca_rest import AlpacaClient, AlpacaError
from indicators import atr, rsi, sma

ET = ZoneInfo("America/New_York")
ACCOUNT = "daily"                 # shares the account with the ORB sleeve
STRATEGY = "mr_rsi2"


# --------------------------------------------------------------------------- #
# Signal
# --------------------------------------------------------------------------- #
def _with_live_close(bars: list[dict], live: float | None) -> list[dict]:
    """Replace TODAY's forming bar close with the live price.

    The daily bars include the still-forming session, whose close is just the
    last print. Every indicator below is defined on CLOSES, so the forming bar's
    close is the live price — the honest live proxy for "evaluated at the close".
    """
    out = [dict(b) for b in bars]
    if not out or live is None or live <= 0:
        return out
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if str(out[-1].get("t", ""))[:10] == today:
        out[-1]["c"] = float(live)
        out[-1]["h"] = max(float(out[-1]["h"]), float(live))
        out[-1]["l"] = min(float(out[-1]["l"]), float(live))
    return out


def signal(bars: list[dict], live: float | None = None) -> dict | None:
    """(entry, stop, target, atr, rsi2, dist_sma200) for a valid MR_rsi2 setup,
    or None. Pure function over bars so tests can pin it against the backtest.

    Mirrors backtest_mr.build_mr_rsi2 exactly:
        uptrend = close > sma200 and sma50 > previous sma50
        entry   = uptrend and rsi2 < rsi_entry
        stop    = close - stop_atr * atr14
    """
    mr = config.MR
    b = _with_live_close(bars, live)
    closes = [float(x["c"]) for x in b]
    need = max(int(mr["trend_sma"]), int(mr["slope_sma"])) + 2
    if len(closes) < need:
        return None
    s200 = sma(closes, int(mr["trend_sma"]))
    s50 = sma(closes, int(mr["slope_sma"]))
    a = atr(b, int(mr["atr_period"]))
    r2 = rsi(closes, int(mr["rsi_period"]))
    i = len(closes) - 1
    if i < 1:
        return None
    v200, v50, v50_prev, va, vr = s200[i], s50[i], s50[i - 1], a[i], r2[i]
    if v200 is None or v50 is None or v50_prev is None or va is None or vr is None:
        return None                      # not enough history yet
    close = closes[i]
    if not (close > v200 and v50 > v50_prev):
        return None                      # not in an uptrend
    if vr >= float(mr["rsi_entry"]):
        return None                      # not a dip
    if va <= 0:
        return None
    stop = close - float(mr["stop_atr"]) * va
    if stop <= 0 or stop >= close:
        return None
    target = close + float(mr["target_atr"]) * va
    return {"entry": close, "stop": stop, "target": target, "atr": va,
            "rsi2": vr, "sma200": v200, "sma50": v50,
            "dist_sma200_pct": (close / v200 - 1) * 100}


def exit_rule_hit(bars: list[dict], live: float | None = None) -> tuple[bool, str]:
    """(True, reason) when the position should be closed by rule rather than by
    its bracket: a close back above SMA(5), or the time stop."""
    mr = config.MR
    b = _with_live_close(bars, live)
    closes = [float(x["c"]) for x in b]
    n = int(mr["exit_sma"])
    if len(closes) < n:
        return False, ""
    s = sma(closes, n)[-1]
    if s is not None and closes[-1] > s:
        return True, f"closed back above its {n}-day average ({closes[-1]:.2f} > {s:.2f})"
    return False, ""


def _sessions_held(trade) -> int:
    """Sessions between entry and now (the time stop counts TRADING days)."""
    stamp = trade["entry_time"] or trade["created_at"]
    try:
        start = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    return max(0, len([d for d in _weekdays_between(start, datetime.now(ET))]))


def _weekdays_between(start: datetime, end: datetime) -> list:
    from datetime import timedelta
    lo, hi = start.date(), end.date()
    out = []
    d = lo
    while d < hi:
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def _open_positions(client) -> dict:
    return {p["symbol"]: p for p in client.positions()}


def _mr_open_trades() -> list:
    return [t for t in db.open_trades(ACCOUNT) if t["strategy"] == STRATEGY]


def _manage_exits(client, dry_run: bool) -> int:
    """Evaluate the rule exit / time stop on every open MR position."""
    closed = 0
    mr = config.MR
    for t in _mr_open_trades():
        sym = t["symbol"]
        try:
            bars = client.bars(sym, timeframe="1Day", limit=260, start=None).get("bars", [])
        except AlpacaError as e:
            print(f"[mr] {sym}: bars error {e}")
            continue
        lt = client.latest_trade(sym)
        live = lt["price"] if lt else None
        hit, why = exit_rule_hit(bars, live)
        held = _sessions_held(t)
        if not hit and held >= int(mr["max_hold_days"]):
            hit, why = True, f"held {held} sessions (time stop {mr['max_hold_days']})"
        if not hit:
            continue
        print(f"[mr] EXIT {sym}: {why}")
        db.log_event(ACCOUNT, STRATEGY, "exit", wording.closed(sym, why), detail=why)
        if dry_run:
            closed += 1
            continue
        try:
            client.release_and_close(sym)
            closed += 1
        except AlpacaError as e:
            print(f"[mr] close error {sym}: {e}")
            db.log_event(ACCOUNT, STRATEGY, "error", f"{sym}: close error: {e}")
    return closed


def _entry_window_now() -> bool:
    """True inside the last-25-minutes window (entries are close-based)."""
    lo, hi = config.MR["entry_window_et"]
    return market.time_in_window(lo, hi)


def _scan_entries(client, equity: float, buying_power: float, dry_run: bool) -> int:
    mr = config.MR
    positions = _open_positions(client)
    taken = 0
    slots = int(mr["max_concurrent"]) - len(_mr_open_trades())
    if slots <= 0:
        db.log_event(ACCOUNT, STRATEGY, "skip",
                     f"Holding {mr['max_concurrent']} positions — the concurrent cap",
                     dedup=True)
        return 0

    allowed, gate_reason = guards.entry_gate(ACCOUNT)
    if not allowed:
        print(f"[mr] blocked: {gate_reason}")
        db.log_event(ACCOUNT, STRATEGY, "blocked", f"Standing down: {gate_reason}", dedup=True)
        return 0
    fomc, fomc_why = guards.fomc_block_new_overnight()
    if fomc:
        db.log_event(ACCOUNT, STRATEGY, "blocked",
                     f"Standing down: {fomc_why} — a rate decision can gap through the stop",
                     dedup=True)
        return 0

    for sym in mr["universe"]:
        if slots <= 0:
            break
        if sym in positions:
            continue
        try:
            bars = client.bars(sym, timeframe="1Day", limit=260, start=None).get("bars", [])
        except AlpacaError as e:
            print(f"[mr] {sym}: bars error {e}")
            continue
        lt = client.latest_trade(sym)
        live = lt["price"] if lt else None
        sig = signal(bars, live)
        if not sig:
            continue

        # Deterministic sizing from the ATR stop, then the shared governor.
        risk_ps = sig["entry"] - sig["stop"]
        risk_dollars = equity * float(mr["risk_pct"])
        qty = max(1, int(risk_dollars / risk_ps)) if risk_ps > 0 else 0
        if qty < 1:
            continue
        notional = qty * sig["entry"]
        cap = equity * float(mr["max_position_pct"])
        if notional > cap:
            qty = max(1, int(cap / sig["entry"]))
            notional = qty * sig["entry"]
        if notional > buying_power:
            qty = max(1, int(buying_power / sig["entry"]))
            notional = qty * sig["entry"]

        size_mult, size_reasons = guards.size_factor(ACCOUNT)
        if size_mult < 1.0:
            qty = max(1, int(qty * size_mult))
            notional = qty * sig["entry"]
            guards.note_size_adjust(ACCOUNT, STRATEGY, size_mult, size_reasons)

        rr = (sig["target"] - sig["entry"]) / risk_ps if risk_ps > 0 else None
        ctx = {
            "strategy": STRATEGY, "symbol": sym, "side": "long",
            "entry": round(sig["entry"], 2), "stop": round(sig["stop"], 2),
            "target": round(sig["target"], 2),
            "risk_reward": round(rr, 2) if rr else None,
            "technical": (f"Mean-reversion dip: RSI(2) {sig['rsi2']:.1f} on a name "
                          f"{(sig['dist_sma200_pct']):+.1f}% above its 200d average, "
                          f"which is in an uptrend; ATR(14) {sig['atr']:.2f} "
                          f"({sig['atr'] / sig['entry'] * 100:.1f}% of price)."),
            "recent_performance": "see trades.db",
        }
        ctx.update(guards.context(ACCOUNT, client, symbols=[sym], with_symbols=False))

        try:
            decision = advisor.decide(ctx)
        except Exception as e:
            print(f"[mr] {sym}: advisor error -> no trade: {e}")
            db.log_event(ACCOUNT, STRATEGY, "error", f"{sym}: advisor error: {e}")
            continue
        if decision["decision"] not in ("go", "size_down"):
            db.log_event(ACCOUNT, STRATEGY, "no_go", f"{sym}: AI: {decision['rationale']}")
            continue
        if decision["decision"] == "size_down":
            qty = max(1, int(qty * decision["size_multiplier"]))

        print(f"[mr] OPEN long {sym} x{qty} @~{sig['entry']:.2f} stop={sig['stop']:.2f} "
              f"target={sig['target']:.2f} (RSI2 {sig['rsi2']:.1f}) | {decision['rationale']}")
        if dry_run:
            taken += 1
            slots -= 1
            continue
        try:
            order = client.bracket_order(sym, qty, "buy", stop_price=sig["stop"],
                                         target_price=sig["target"], time_in_force="gtc")
        except AlpacaError as e:
            print(f"[mr] order error {sym}: {e}")
            db.log_event(ACCOUNT, STRATEGY, "error", f"{sym}: order error: {e}")
            continue

        db.insert_trade(
            account=ACCOUNT, strategy=STRATEGY, symbol=sym, asset_class="etf",
            side="long", qty=qty, entry_price=sig["entry"], status="open",
            rr_planned=(round(rr, 2) if rr else None), stop_price=sig["stop"],
            target_price=sig["target"], order_id=order.get("id"),
            client_order_id=order.get("client_order_id"),
            note=(f"RSI(2) {sig['rsi2']:.1f} dip in an uptrend; ATR stop "
                  f"{sig['atr']:.2f} | {decision['rationale']}"[:200]),
            decision_json=json.dumps({
                "decision": decision["decision"], "rationale": decision["rationale"],
                "size_multiplier": decision["size_multiplier"], "context": ctx,
                "signal": {k: (round(v, 4) if isinstance(v, float) else v)
                           for k, v in sig.items()},
            }),
        )
        db.log_event(ACCOUNT, STRATEGY, "go",
                     wording.opened(sym, "long", qty,
                                    float(decision.get("size_multiplier") or 1.0) * size_mult),
                     detail=(f"RSI(2) {sig['rsi2']:.1f} · entry~{sig['entry']:.2f} "
                             f"stop={sig['stop']:.2f} target={sig['target']:.2f} "
                             f"| {decision['rationale']}"))
        taken += 1
        slots -= 1
        positions[sym] = {"symbol": sym}
    return taken


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init_db()
    if not config.MR.get("enabled", True):
        print("[mr] disabled in config")
        return
    client = AlpacaClient(ACCOUNT)
    try:
        clock = client.clock()
    except AlpacaError as e:
        print(f"[mr] clock error: {e}")
        db.log_event(ACCOUNT, STRATEGY, "error", f"clock error: {e}", dedup=True)
        return
    if not clock.get("is_open"):
        print("[mr] market closed")
        db.log_event(ACCOUNT, STRATEGY, "skip", "market closed", dedup=True)
        return

    try:
        acct = client.account()
    except AlpacaError as e:
        print(f"[mr] account error: {e}")
        db.log_event(ACCOUNT, STRATEGY, "error", f"account error: {e}", dedup=True)
        return
    equity = float(acct.get("equity", 0.0) or 0.0)
    buying_power = float(acct.get("buying_power", 0.0) or 0.0)
    if equity <= 0:
        print("[mr] zero equity")
        return

    # Exits run on every tick; entries only inside the close-based window.
    n_exit = _manage_exits(client, args.dry_run)
    taken = 0
    if _entry_window_now():
        taken = _scan_entries(client, equity, buying_power, args.dry_run)
    else:
        lo, hi = config.MR["entry_window_et"]
        db.log_event(ACCOUNT, STRATEGY, "skip",
                     f"Waiting for the closing window ({lo}-{hi} ET) — this signal is "
                     f"evaluated on the closing price", dedup=True)
    print(f"[mr] exits {n_exit}, entries {taken} | equity {equity:,.2f}")


if __name__ == "__main__":
    main()
