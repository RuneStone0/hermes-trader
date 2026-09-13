"""Daily SPY Opening-Range Breakout trader. Idempotent — safe to call every few
minutes during market hours via cron.

Flow per tick: compute the ORB (09:30-10:00), detect a breakout with close
confirmation, size for 1% risk with a 2:1 target, gate through the LLM advisor,
and place a GTC bracket order. NO forced flatten: a position may be held
overnight — the stop/target legs (GTC) protect it, and the LLM decides when to
exit. One decision per today (loose cap).

Usage: python3 daily_run.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import advisor
import config
import db
import wording
import fees
import market
from alpaca_rest import AlpacaClient, AlpacaError

ET = ZoneInfo("America/New_York")
ACCOUNT = "daily"
SYMBOL = config.DAILY["symbol"]


def _date() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _state_path(d: str) -> Path:
    return config.STATE_DIR / f"daily_{d}.json"


def _load_state(d: str) -> dict:
    p = _state_path(d)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(d: str, s: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(d).write_text(json.dumps(s, indent=2))


def _orb_setup(bars: list, equity: float) -> dict | None:
    orb = [b for b in bars if market.bar_et_hm(b["t"]) <= "10:00"]
    post = [b for b in bars if market.bar_et_hm(b["t"]) > "10:00"]
    if not orb or not post:
        return None
    hi = max(b["h"] for b in orb)
    lo = min(b["l"] for b in orb)
    rng = hi - lo
    if rng <= 0:
        return None
    entry = side = None
    confirm = config.DAILY.get("close_confirmation", True)
    for b in post:
        if confirm:  # require a bar CLOSE beyond the range (classic ORB)
            if b["c"] > hi:
                entry, side = hi, "long"
                break
            if b["c"] < lo:
                entry, side = lo, "short"
                break
        else:  # any wick beyond the range counts as a breakout
            if b["h"] > hi:
                entry, side = hi, "long"
                break
            if b["l"] < lo:
                entry, side = lo, "short"
                break
    if entry is None:
        return None
    stop = lo if side == "long" else hi
    mult = config.DAILY["rr_multiple"]
    target = entry + mult * rng if side == "long" else entry - mult * rng
    risk_dollars = equity * config.RISK_PCT_PER_TRADE
    shares = max(1, int(risk_dollars / rng))
    return {"entry": entry, "stop": stop, "target": target, "side": side,
            "shares": shares, "range": rng}


def _regime(client: AlpacaClient) -> str:
    try:
        bars = client.bars(SYMBOL, timeframe="1Day", limit=25, start=None).get("bars", [])
        if len(bars) < 21:
            return "insufficient data"
        closes = [float(b["c"]) for b in bars]
        sma20 = sum(closes[-20:]) / 20
        last = closes[-1]
        slope = (closes[-1] - closes[-10]) / closes[-10] if closes[-10] else 0.0
        trend = "above" if last > sma20 else "below"
        direction = "rising" if slope > 0.003 else ("falling" if slope < -0.003 else "flat")
        return f"SPY {trend} 20d-SMA ({last:.2f} vs {sma20:.2f}), 10d {direction}"
    except Exception as e:
        return f"regime unavailable: {e}"


def _recent_perf() -> str:
    try:
        closed = [t for t in db.all_trades(ACCOUNT) if t["status"] == "closed"]
        tail = closed[-5:]
        if not tail:
            return "no closed trades yet"
        parts = [f"{'W' if (t['net_pnl'] or 0) > 0 else 'L'}" for t in tail]
        return "last " + ",".join(parts)
    except Exception:
        return "unavailable"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init_db()
    client = AlpacaClient(ACCOUNT)
    try:
        clock = client.clock()
    except AlpacaError as e:
        print(f"[daily] clock error: {e}")
        db.log_event(ACCOUNT, "daily_orb", "error", f"clock error: {e}", dedup=True)
        return
    if not clock.get("is_open"):
        print("[daily] market closed")
        db.log_event(ACCOUNT, "daily_orb", "skip", "market closed", dedup=True)
        return

    d = _date()
    hm = market.et_hm()
    st = _load_state(d)
    equity = float(client.account().get("equity", 10000.0))

    positions = client.positions()

    # 2. Already decided today (loose cap, not a hard strategy rule).
    if st.get("decided"):
        db.log_event(ACCOUNT, "daily_orb", "skip", "already decided today", dedup=True)
        return

    # 3. Wait for the ORB window to complete.
    if hm <= config.DAILY["orb_end"]:
        db.log_event(ACCOUNT, "daily_orb", "skip", "waiting for opening range", dedup=True)
        return

    # 4. Evaluate the breakout setup.
    bars = client.bars(SYMBOL, timeframe="5Min", limit=80, start=d).get("bars", [])
    setup = _orb_setup(bars, equity)
    if not setup:
        db.log_event(ACCOUNT, "daily_orb", "skip", "no breakout yet today", dedup=True)
        return  # no breakout yet today; keep waiting

    # 5. Fee-adjusted R:R gate.
    rr = fees.fee_adjusted_rr(setup["entry"], setup["target"], setup["stop"],
                              setup["side"], setup["shares"])
    if not rr["clears"]:
        st.update(decided=True, decision="no_go", note="net R:R below threshold after fees")
        _save_state(d, st)
        print(f"[daily] no-go: fee-adjusted R:R {rr['net_rr']} < {config.MIN_NET_RR}")
        db.log_event(ACCOUNT, "daily_orb", "no_go",
                     f"net R:R {rr['net_rr']} below floor {config.MIN_NET_RR} after fees")
        return

    # 6. LLM decision gate.
    ctx = {
        "strategy": "daily_orb", "symbol": SYMBOL, "side": setup["side"],
        "entry": round(setup["entry"], 2), "stop": round(setup["stop"], 2),
        "target": round(setup["target"], 2), "risk_reward": rr["gross_rr"],
        "net_rr_after_fees": rr["net_rr"],
        "technical": (f"Opening-range ({config.DAILY['orb_start']}-{config.DAILY['orb_end']}) "
                      f"{setup['side']} breakout; range ${setup['range']:.2f}; "
                      f"close beyond {setup['entry']:.2f}."),
        "market_regime": _regime(client),
        "recent_performance": _recent_perf(),
        "news": "no news feed (requires Alpaca data subscription)",
    }
    try:
        decision = advisor.decide(ctx)
    except Exception as e:
        st.update(decided=True, decision="no_go", note=f"advisor error: {e}")
        _save_state(d, st)
        print(f"[daily] advisor error -> no trade: {e}")
        db.log_event(ACCOUNT, "daily_orb", "error", f"advisor error: {e}")
        return

    if decision["decision"] not in ("go", "size_down"):
        st.update(decided=True, decision="no_go", note=decision["rationale"])
        _save_state(d, st)
        print(f"[daily] NO-GO: {decision['rationale']}")
        db.log_event(ACCOUNT, "daily_orb", "no_go", f"AI: {decision['rationale']}")
        return

    shares = setup["shares"]
    if decision["decision"] == "size_down":
        shares = max(1, int(shares * decision["size_multiplier"]))

    print(f"[daily] {decision['decision'].upper()} {setup['side']} {SYMBOL} x{shares} "
          f"entry~{setup['entry']:.2f} stop={setup['stop']:.2f} target={setup['target']:.2f} "
          f"| {decision['rationale']}")

    if args.dry_run:
        return

    try:
        order = client.bracket_order(
            SYMBOL, shares, "buy" if setup["side"] == "long" else "sell",
            stop_price=setup["stop"], target_price=setup["target"],
            time_in_force=config.DAILY.get("time_in_force", "gtc"))
    except AlpacaError as e:
        st.update(decided=True, decision="error", note=f"order error: {e}")
        _save_state(d, st)
        print(f"[daily] order error: {e}")
        return

    db.insert_trade(
        account=ACCOUNT, strategy="daily_orb", symbol=SYMBOL, asset_class="etf",
        side=setup["side"], qty=shares, entry_price=setup["entry"], status="open",
        rr_planned=rr["gross_rr"], stop_price=setup["stop"],
        target_price=setup["target"], order_id=order.get("id"),
        client_order_id=order.get("client_order_id"), note=decision["rationale"],
        decision_json=json.dumps({
            "decision": decision["decision"],
            "rationale": decision["rationale"],
            "size_multiplier": decision["size_multiplier"],
            "context": ctx,
        }),
    )
    st.update(decided=True, decision=decision["decision"], order_id=order.get("id"))
    _save_state(d, st)
    print(f"[daily] bracket order placed: {order.get('id')}")
    db.log_event(ACCOUNT, "daily_orb", "go",
                 wording.opened(SYMBOL, setup["side"], shares,
                                decision.get("size_multiplier")),
                 detail=(f"entry~{setup['entry']:.2f} stop={setup['stop']:.2f} "
                         f"target={setup['target']:.2f} | {decision['rationale']}"))


if __name__ == "__main__":
    main()
