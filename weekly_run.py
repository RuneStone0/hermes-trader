"""Weekly SPY trend-following pullback trader. Idempotent — safe to call daily.

Logic: trend from price vs 20-week (≈100d) SMA. In an uptrend, buy a pullback to
within 1x daily-ATR of the SMA; in a downtrend, short a rally to within 1x ATR.
Stop = 1x ATR, target = 2x ATR (2:1), hold up to max_hold_days. One position.

Usage: python3 weekly_run.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import advisor
import config
import db
import fees
import market
from alpaca_rest import AlpacaClient, AlpacaError
from indicators import atr, sma

ET = ZoneInfo("America/New_York")
ACCOUNT = "weekly"
SYMBOL = config.WEEKLY["symbol"]


def _state_path() -> Path:
    return config.STATE_DIR / "weekly.json"


def _load_state() -> dict:
    p = _state_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(s: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(s, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init_db()
    client = AlpacaClient(ACCOUNT)
    try:
        clock = client.clock()
    except AlpacaError as e:
        print(f"[weekly] clock error: {e}")
        db.log_event(ACCOUNT, "weekly_pullback", "error", f"clock error: {e}", dedup=True)
        return
    if not clock.get("is_open"):
        print("[weekly] market closed")
        db.log_event(ACCOUNT, "weekly_pullback", "skip", "market closed", dedup=True)
        return

    equity = float(client.account().get("equity", 10000.0))
    positions = {p["symbol"]: p for p in client.positions()}
    open_trades = db.open_trades(ACCOUNT)

    # 1. Time-stop: close positions held longer than max_hold_days.
    if SYMBOL in positions:
        held = open_trades[0] if open_trades else None
        if held and held["entry_time"]:
            try:
                et = datetime.fromisoformat(held["entry_time"].replace("Z", "+00:00"))
                if datetime.now(et.tzinfo) - et > timedelta(days=config.WEEKLY["max_hold_days"]):
                    print(f"[weekly] max hold exceeded, closing {SYMBOL}")
                    db.log_event(ACCOUNT, "weekly_pullback", "exit",
                                 f"closed {SYMBOL}: max hold ({config.WEEKLY['max_hold_days']}d) exceeded")
                    if not args.dry_run:
                        client.cancel_all()
                        client.close_position(SYMBOL)
                    return
            except ValueError:
                pass
        db.log_event(ACCOUNT, "weekly_pullback", "skip", "already positioned", dedup=True)
        return  # already positioned

    # 2. Fetch daily bars for trend + ATR.
    bars = client.bars(SYMBOL, timeframe="1Day", limit=140, start=None).get("bars", [])
    if len(bars) < 120:
        print("[weekly] insufficient daily bars")
        db.log_event(ACCOUNT, "weekly_pullback", "error", "insufficient daily bars")
        return
    closes = [float(b["c"]) for b in bars]
    sma100 = sma(closes, 100)[-1]
    atr14 = atr(bars, 14)[-1]
    if sma100 is None or atr14 is None or atr14 <= 0:
        print("[weekly] insufficient indicator data")
        db.log_event(ACCOUNT, "weekly_pullback", "error", "insufficient indicator data")
        return
    last = closes[-1]

    # 3. Trend + pullback signal.
    setup = None
    tgt = config.WEEKLY["target_atr"]
    if last > sma100 and last <= sma100 + atr14:          # uptrend, pulled back to SMA
        entry = last
        stop = entry - atr14
        target = entry + tgt * atr14
        setup = {"side": "long", "entry": entry, "stop": stop, "target": target,
                 "trend": "uptrend"}
    elif last < sma100 and last >= sma100 - atr14:        # downtrend, rallied to SMA
        entry = last
        stop = entry + atr14
        target = entry - tgt * atr14
        setup = {"side": "short", "entry": entry, "stop": stop, "target": target,
                 "trend": "downtrend"}
    if not setup:
        db.log_event(ACCOUNT, "weekly_pullback", "skip", "no pullback to SMA today", dedup=True)
        return

    risk_dollars = equity * config.RISK_PCT_PER_TRADE
    shares = max(1, int(risk_dollars / atr14))

    rr = fees.fee_adjusted_rr(setup["entry"], setup["target"], setup["stop"],
                              setup["side"], shares)
    if not rr["clears"]:
        print(f"[weekly] no-go: fee-adjusted R:R {rr['net_rr']} < {config.MIN_NET_RR}")
        db.log_event(ACCOUNT, "weekly_pullback", "no_go",
                     f"net R:R {rr['net_rr']} below floor {config.MIN_NET_RR} after fees")
        return

    ctx = {
        "strategy": "weekly_pullback", "symbol": SYMBOL, "side": setup["side"],
        "entry": round(setup["entry"], 2), "stop": round(setup["stop"], 2),
        "target": round(setup["target"], 2), "risk_reward": rr["gross_rr"],
        "net_rr_after_fees": rr["net_rr"],
        "technical": (f"{setup['trend']}: price {last:.2f} vs 100d-SMA {sma100:.2f}, "
                      f"pulled back to within 1x ATR ({atr14:.2f})."),
        "market_regime": setup["trend"],
        "recent_performance": "see trades.db",
        "news": "no news feed (requires Alpaca data subscription)",
    }
    try:
        decision = advisor.decide(ctx)
    except Exception as e:
        print(f"[weekly] advisor error -> no trade: {e}")
        db.log_event(ACCOUNT, "weekly_pullback", "error", f"advisor error: {e}")
        return

    if decision["decision"] not in ("go", "size_down"):
        print(f"[weekly] NO-GO: {decision['rationale']}")
        db.log_event(ACCOUNT, "weekly_pullback", "no_go", f"LLM: {decision['rationale']}")
        return

    shares2 = shares
    if decision["decision"] == "size_down":
        shares2 = max(1, int(shares * decision["size_multiplier"]))

    print(f"[weekly] {decision['decision'].upper()} {setup['side']} {SYMBOL} x{shares2} "
          f"entry~{setup['entry']:.2f} stop={setup['stop']:.2f} target={setup['target']:.2f} "
          f"| {decision['rationale']}")
    if args.dry_run:
        return

    try:
        order = client.bracket_order(
            SYMBOL, shares2, "buy" if setup["side"] == "long" else "sell",
            stop_price=setup["stop"], target_price=setup["target"],
            entry_type="limit", entry_price=setup["entry"], time_in_force="gtc")
    except AlpacaError as e:
        print(f"[weekly] order error: {e}")
        return

    db.insert_trade(
        account=ACCOUNT, strategy="weekly_pullback", symbol=SYMBOL, asset_class="etf",
        side=setup["side"], qty=shares2, entry_price=setup["entry"], status="open",
        rr_planned=rr["gross_rr"], stop_price=setup["stop"],
        target_price=setup["target"], order_id=order.get("id"),
        client_order_id=order.get("client_order_id"), note=decision["rationale"],
    )
    _save_state({"last_order_id": order.get("id"), "side": setup["side"],
                 "entry": setup["entry"], "placed_at": datetime.now(ET).isoformat()})
    print(f"[weekly] bracket order placed: {order.get('id')}")
    db.log_event(ACCOUNT, "weekly_pullback", "go",
                 f"{decision['decision']} {setup['side']} {SYMBOL} x{shares2}",
                 detail=(f"entry~{setup['entry']:.2f} stop={setup['stop']:.2f} "
                         f"target={setup['target']:.2f} | {decision['rationale']}"))


if __name__ == "__main__":
    main()
