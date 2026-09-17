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
import guards
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
    risk_dollars = equity * config.DAILY.get("risk_pct", config.RISK_PCT_PER_TRADE)
    shares = max(1, int(risk_dollars / rng))
    # `last` = the current 5-min close. The plan's entry is the RANGE EDGE, but
    # the order is a market order, so it fills at whatever the tape says NOW —
    # and the stop stays pinned at the opposite side of the range. Re-checking
    # every tick from 10:00 onward means a 10:05 breakout found at 15:30 was
    # planned as a 1x-range risk and would actually be entered several ranges
    # away from its own stop. chase_pct guards that: too far past the edge and
    # the setup is STALE — pass on it rather than buy the top of the move.
    last = float(post[-1]["c"])
    chase_pct = (last - entry) / entry * 100 if entry else 0.0
    if side == "short":
        chase_pct = -chase_pct
    return {"entry": entry, "stop": stop, "target": target, "side": side,
            "shares": shares, "range": rng, "last": last, "chase_pct": chase_pct,
            "stale": chase_pct > float(config.DAILY.get("max_chase_pct", 0.25))}


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
    if setup["stale"]:
        # A breakout this old is no longer an opening-range breakout: entering
        # now means paying a price the stop (pinned to the opposite edge) was
        # never sized for. Pass, and say why — silently skipping would hide a
        # rule that fires almost every day.
        st.update(decided=True, decision="no_go", note="breakout went stale")
        _save_state(d, st)
        db.log_event(ACCOUNT, "daily_orb", "no_go",
                     f"Breakout is stale: price is {setup['chase_pct']:.2f}% past the "
                     f"{setup['entry']:.2f} range edge (limit {config.DAILY.get('max_chase_pct')}%)")
        return

    # 4b. One position per symbol — the existing bracket already owns the exit.
    #     (Without this, a second ORB entry could stack on a symbol still held
    #     from a previous session, with a second bracket whose legs only cover
    #     the new shares.)
    if SYMBOL in {p["symbol"]: p for p in positions}:
        db.log_event(ACCOUNT, "daily_orb", "skip", f"already holding {SYMBOL}", dedup=True)
        return

    # 5. Deterministic gates BEFORE spending an LLM call: the drawdown governor
    #    and the economic-calendar blackout. Cheap, auditable, and a refusal is
    #    journaled so the dashboard shows why the setup was held back.
    allowed, gate_reason = guards.entry_gate(ACCOUNT)
    if not allowed:
        st.update(decided=True, decision="blocked", note=gate_reason)
        _save_state(d, st)
        print(f"[daily] blocked: {gate_reason}")
        db.log_event(ACCOUNT, "daily_orb", "blocked", f"Standing down: {gate_reason}")
        return

    fomc, fomc_why = guards.fomc_block_new_overnight()
    if fomc:
        st.update(decided=True, decision="blocked", note=fomc_why)
        _save_state(d, st)
        db.log_event(ACCOUNT, "daily_orb", "blocked",
                     f"Standing down: {fomc_why} — a rate decision can gap "
                     f"straight through the stop")
        return

    # 6. Fee-adjusted R:R gate.
    rr = fees.fee_adjusted_rr(setup["entry"], setup["target"], setup["stop"],
                              setup["side"], setup["shares"])
    if not rr["clears"]:
        st.update(decided=True, decision="no_go", note="net R:R below threshold after fees")
        _save_state(d, st)
        print(f"[daily] no-go: fee-adjusted R:R {rr['net_rr']} < {config.MIN_NET_RR}")
        db.log_event(ACCOUNT, "daily_orb", "no_go",
                     f"net R:R {rr['net_rr']} below floor {config.MIN_NET_RR} after fees")
        return

    # 7. LLM decision gate. The context is assembled by guards.context() so all
    #    three bots see the SAME fields (regime, calendar, news, drawdown,
    #    benchmark) in the same shape.
    ctx = {
        "strategy": "daily_orb", "symbol": SYMBOL, "side": setup["side"],
        "entry": round(setup["entry"], 2), "stop": round(setup["stop"], 2),
        "target": round(setup["target"], 2), "risk_reward": rr["gross_rr"],
        "net_rr_after_fees": rr["net_rr"],
        "technical": (f"Opening-range ({config.DAILY['orb_start']}-{config.DAILY['orb_end']}) "
                      f"{setup['side']} breakout; range ${setup['range']:.2f}; "
                      f"close beyond {setup['entry']:.2f}; now {setup['last']:.2f} "
                      f"({setup['chase_pct']:+.2f}% past the edge)."),
        "recent_performance": _recent_perf(),
    }
    ctx.update(guards.context(ACCOUNT, client, symbols=[SYMBOL]))
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

    # Risk-governor / event-day sizing. Applied AFTER the LLM's own size_down so
    # the two are multiplicative — every actor here can only reduce size.
    size_mult, size_reasons = guards.size_factor(ACCOUNT)
    if size_mult < 1.0:
        shares = max(1, int(shares * size_mult))
        guards.note_size_adjust(ACCOUNT, "daily_orb", size_mult, size_reasons)

    print(f"[daily] {decision['decision'].upper()} {setup['side']} {SYMBOL} x{shares} "
          f"entry~{setup['entry']:.2f} stop={setup['stop']:.2f} target={setup['target']:.2f} "
          f"| {decision['rationale']}"
          + (f" | size {size_mult:.0%}" if size_mult < 1.0 else ""))

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
                                float(decision.get("size_multiplier") or 1.0) * size_mult),
                 detail=(f"entry~{setup['entry']:.2f} stop={setup['stop']:.2f} "
                         f"target={setup['target']:.2f} | {decision['rationale']}"))


if __name__ == "__main__":
    main()
