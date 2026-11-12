"""Portfolio sleeve — multi-symbol pullback on the lab-validated universe.

Why this exists
---------------
The single-symbol sleeves each trade ONE instrument, and the strategy lab showed
that the validated edge lives in BREADTH: the winning pullback config measured
+38.13R over 166 trades across 14 ETFs, but +5.83R over 13 trades on SPY alone.
A one-symbol sleeve structurally cannot capture that edge. This sleeve trades the
whole universe, so it can.

The rule (champion of strategy_lab profile=weekly, measured on real bars):
    entry  : close > SMA50  AND  close <= SMA50 + 1.0 * ATR14     (uptrend dip)
    stop   : entry - 1.5 * ATR14
    target : entry + 2.5 * ATR14
    max hold: 42 calendar days (~30 trading days)
Evidence (14-ETF portfolio, fees + slippage modelled):
    IS 2018-11..2024-12 +55.31R | holdout A 2025 +28.24R | B 2026 +9.89R
    pooled OOS +38.13R, PF 1.50, n=166.  Positive in EVERY window.

DELIBERATELY NO LLM VETO. The lab measured the raw rule; adding a filter would
change the system and void the measurement. The old weekly sleeve is the warning:
its LLM veto layer turned a live signal into zero trades in its entire lifetime.

Sizing, and why it is not 1%-risk:
    Alpaca rejects fractional bracket orders ("fractional orders must have
    order_class=simple", verified 2026-11-12), so every position is WHOLE shares.
    On a $10k account that makes risk-per-trade lumpy (one SPY share is 7.7% of
    equity), so positions are sized by NOTIONAL weight instead: 12.5% each, max 8
    slots, total gross <= 100% of equity. No leverage is used even though the
    account has 4x day-trading buying power.

Usage: python3 portfolio_run.py [--dry-run]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import config
import db
import guards
from alpaca_rest import AlpacaClient, AlpacaError
from indicators import atr, sma

ACCOUNT = "weekly"
STRATEGY = "portfolio_pullback"

PARAMS = {"sma_n": 50, "band": 1.0, "stop_atr": 1.5, "tgt_atr": 2.5,
          "max_hold_days": 42}

UNIVERSE = ["SPY", "QQQ", "IWM", "GLD", "TLT", "XLE", "XLF", "XLV",
            "XLU", "XLP", "XLB", "XLK", "SMH", "SLV"]

MAX_POSITIONS = 8         # breadth without needing >8 concurrent brackets
WEIGHT = 0.125            # target notional per position (8 x 12.5% = 100%)
MAX_GROSS = 1.00          # total notional <= equity; no leverage
MIN_SHARE_FRACTION = 0.25 # skip if 1 share alone exceeds 25% of equity


def _close_positions_past_max_hold(client, positions) -> None:
    """Time stop. The bracket handles stop/target; nothing handled 'held too long'."""
    now = datetime.now(timezone.utc)
    for t in db.open_trades(ACCOUNT):
        sym = t["symbol"]
        if sym not in positions:
            continue
        try:
            opened = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
        except Exception:
            continue
        age = (now - opened).days
        if age < PARAMS["max_hold_days"]:
            continue
        size = len(positions)
        try:
            _, released = client.release_and_close(sym)
            print(f"[portfolio] TIME STOP {sym} after {age}d (released {released} leg(s), "
                  f"was 1 of {size} positions)")
            db.log_event(ACCOUNT, STRATEGY, "exit",
                         f"Closed {sym} — time stop after {age}d (max hold "
                         f"{PARAMS['max_hold_days']}d)",
                         detail=f"rule max_hold_days={PARAMS['max_hold_days']}, "
                                f"held {age} calendar days")
        except AlpacaError as e:
            print(f"[portfolio] time-stop close failed for {sym}: {e}")
            db.log_event(ACCOUNT, STRATEGY, "error",
                         f"time-stop close failed for {sym}: {e}")


def _signal(bars) -> dict | None:
    """The champion rule, computed exactly as the lab computed it."""
    closes = [b["c"] for b in bars]
    s = sma(closes, PARAMS["sma_n"])
    a = atr(bars, 14)
    if len(closes) < PARAMS["sma_n"] + 2 or s[-1] is None or a[-1] is None or a[-1] <= 0:
        return None
    last, sma_v, atr_v = closes[-1], s[-1], a[-1]
    if not (last > sma_v and last <= sma_v + PARAMS["band"] * atr_v):
        return None
    stop = last - PARAMS["stop_atr"] * atr_v
    target = last + PARAMS["tgt_atr"] * atr_v
    if stop <= 0 or target <= last:
        return None
    return {"symbol": None, "entry": last, "stop": stop, "target": target,
            "sma": sma_v, "atr": atr_v, "dist_atr": (last - sma_v) / atr_v}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--assume-open", action="store_true",
                    help="TEST ONLY: skip the market-clock gate so the scan can be "
                         "exercised outside RTH. Never set this in the job.")
    args = ap.parse_args()

    db.init_db()
    client = AlpacaClient(ACCOUNT)

    if not args.assume_open:
        try:
            clock = client.clock()
        except AlpacaError as e:
            print(f"[portfolio] clock error: {e}")
            db.log_event(ACCOUNT, STRATEGY, "error", f"clock error: {e}", dedup=True)
            return
        if not clock.get("is_open"):
            db.log_event(ACCOUNT, STRATEGY, "skip", "market closed", dedup=True)
            return

    positions = {p["symbol"]: p for p in client.positions()}

    # 1. exits we own (brackets handle stop/target; max-hold is ours)
    _close_positions_past_max_hold(client, positions)
    positions = {p["symbol"]: p for p in client.positions()}

    acct = client.account()
    equity = float(acct.get("equity") or 0.0)
    if equity <= 0:
        db.log_event(ACCOUNT, STRATEGY, "error", "equity unavailable")
        return
    gross = sum(abs(float(p.get("market_value") or 0.0)) for p in positions.values())
    slots = MAX_POSITIONS - len(positions)
    if slots <= 0:
        db.log_event(ACCOUNT, STRATEGY, "skip",
                     f"portfolio full: {len(positions)}/{MAX_POSITIONS} positions",
                     dedup=True)
        return

    allowed, gate_reason = guards.entry_gate(ACCOUNT)
    if not allowed:
        print(f"[portfolio] blocked: {gate_reason}")
        db.log_event(ACCOUNT, STRATEGY, "blocked", f"Standing down: {gate_reason}")
        return
    fomc, fomc_why = guards.fomc_block_new_overnight()
    if fomc:
        db.log_event(ACCOUNT, STRATEGY, "blocked",
                     f"Standing down: {fomc_why} — a rate decision can gap "
                     f"straight through the stop")
        return

    # 2. scan the universe for the rule
    cands = []
    for sym in UNIVERSE:
        if sym in positions:
            continue
        try:
            payload = client.bars(sym, timeframe="1Day", limit=PARAMS["sma_n"] + 80)
            bars = [{"c": float(b["c"]), "h": float(b["h"]), "l": float(b["l"])}
                    for b in (payload.get("bars") or [])]
        except AlpacaError as e:
            print(f"[portfolio] {sym}: bars error {e}")
            continue
        sig = _signal(bars)
        if sig:
            sig["symbol"] = sym
            cands.append(sig)

    if not cands:
        db.log_event(ACCOUNT, STRATEGY, "skip",
                     f"no setups in {len(UNIVERSE)} symbols "
                     f"(close in [SMA50, SMA50+1.0 ATR])", dedup=True)
        print(f"[portfolio] no setups across {len(UNIVERSE)} symbols")
        return

    # tightest-to-the-SMA first: that is the deepest pullback in an uptrend
    cands.sort(key=lambda c: c["dist_atr"])
    print(f"[portfolio] {len(cands)} setup(s), {slots} slot(s), gross ${gross:,.0f} "
          f"of ${equity:,.0f}")

    opened = 0
    for sig in cands:
        if slots <= 0 or gross >= equity * MAX_GROSS:
            break
        sym, px = sig["symbol"], sig["entry"]
        budget = min(equity * WEIGHT, equity * MAX_GROSS - gross)
        shares = int(budget // px)
        if shares < 1:
            if px <= equity * MIN_SHARE_FRACTION:
                print(f"[portfolio] {sym}: 1 share (${px:.0f}) exceeds budget "
                      f"${budget:.0f} — skipping")
            continue
        if shares * px > equity * MIN_SHARE_FRACTION:
            print(f"[portfolio] {sym}: would be {shares * px / equity:.0%} of equity "
                  f"in one name — too concentrated, skipping")
            continue
        notional = shares * px
        print(f"[portfolio] BUY {sym} x{shares} @~{px:.2f}  stop={sig['stop']:.2f} "
              f"target={sig['target']:.2f}  notional=${notional:,.0f} "
              f"({notional / equity:.1%})  pullback={sig['dist_atr']:.2f} ATR")
        if args.dry_run:
            opened += 1
            gross += notional
            slots -= 1
            continue
        try:
            order = client.bracket_order(
                sym, shares, "buy",
                stop_price=round(sig["stop"], 2), target_price=round(sig["target"], 2),
                entry_type="market", time_in_force="gtc")
        except AlpacaError as e:
            print(f"[portfolio] {sym}: order error {e}")
            db.log_event(ACCOUNT, STRATEGY, "error", f"{sym}: order error {e}")
            continue
        db.insert_trade(
            account=ACCOUNT, strategy=STRATEGY, symbol=sym, asset_class="etf",
            side="long", qty=shares, entry_price=px, status="open",
            rr_planned=PARAMS["tgt_atr"] / PARAMS["stop_atr"],
            stop_price=round(sig["stop"], 2), target_price=round(sig["target"], 2),
            order_id=order.get("id"), client_order_id=order.get("client_order_id"),
            note=(f"rule: close in [SMA{PARAMS['sma_n']}, "
                  f"SMA{PARAMS['sma_n']}+{PARAMS['band']}ATR]; "
                  f"close {px:.2f} vs SMA {sig['sma']:.2f}, ATR {sig['atr']:.2f}, "
                  f"pullback {sig['dist_atr']:.2f} ATR"))
        db.log_event(ACCOUNT, STRATEGY, "go", f"Bought {shares} {sym}",
                     detail=(f"portfolio pullback: close {px:.2f} vs SMA"
                             f"{PARAMS['sma_n']} {sig['sma']:.2f} "
                             f"(within {PARAMS['band']} ATR of {sig['atr']:.2f}); "
                             f"stop {sig['stop']:.2f} target {sig['target']:.2f}; "
                             f"{notional / equity:.1%} of equity"))
        opened += 1
        gross += notional
        slots -= 1

    if opened == 0:
        db.log_event(ACCOUNT, STRATEGY, "skip",
                     f"{len(cands)} setup(s) but none sized/slotted", dedup=True)
    print(f"[portfolio] opened {opened} position(s); gross now ${gross:,.0f} "
          f"of ${equity:,.0f}")


if __name__ == "__main__":
    main()
