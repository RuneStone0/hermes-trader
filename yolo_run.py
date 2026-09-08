"""YOLO — the fully-autonomous trader, self-contained (no Hermes/MCP).

Each tick makes ONE DeepSeek call returning a JSON action plan (open/close/hold).
This script validates every action against the safety floor, sizes it for bounded
risk, and executes via bracket orders. Fail-closed: any error or invalid action is
skipped, never force-executed.

Usage: python3 yolo_run.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json

import advisor
import config
import db
from alpaca_rest import AlpacaClient, AlpacaError

ACCOUNT = "yolo"

# ETFs in the watchlist (for the db asset_class label); everything else = equity.
ETF_SYMBOLS = {
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "IEF", "HYG",
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "SMH",
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _to_price(v, ref: float):
    """Interpret a level as an absolute price, or a signed % move from ref
    (e.g. '-1.5%' = 1.5% below ref, '+3%' = 3% above ref)."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, str) and v.strip().endswith("%"):
            pct = float(v.strip()[:-1]) / 100.0
            return ref * (1 + pct)
        return float(v)
    except (TypeError, ValueError):
        return None


def _ref_price(client: AlpacaClient, sym: str) -> float | None:
    """Live execution reference = the broker's LATEST TRADE price.

    Alpaca validates bracket legs against the live price at submission (a
    stop within $0.01 of it -> HTTP 422), so a stale daily close is NOT a valid
    reference (MSFT 2026-09-08: close 510.12 vs live 490.56 -> stop rejected).
    Falls back to the last daily close only if the live feed is unavailable;
    the geometry guards below still protect against a stale level.
    """
    lt = client.latest_trade(sym)
    if lt and lt.get("price", 0) > 0:
        return lt["price"]
    try:
        bars = client.bars(sym, timeframe="1Day", limit=5, start=None).get("bars", [])
    except AlpacaError:
        return None
    return float(bars[-1]["c"]) if bars else None


def _open_action(client, a, equity, buying_power, positions, dry_run) -> bool:
    sym = str(a.get("symbol", "")).strip().upper()
    if not sym:
        return False
    # Freedom: allow ANY retail US equity/ETF — reject only crypto and options
    # (crypto needs a separate data feed; options need a contract chain).
    if "/" in sym:
        print(f"[yolo] reject {sym}: crypto (BTC-only allowlist, separate feed)")
        db.log_event(ACCOUNT, "yolo", "no_go", f"{sym}: crypto not supported")
        return False
    if len(sym) >= 21:
        print(f"[yolo] reject {sym}: options not supported")
        db.log_event(ACCOUNT, "yolo", "no_go", f"{sym}: options not supported")
        return False
    if sym in positions:
        print(f"[yolo] skip {sym}: already positioned")
        db.log_event(ACCOUNT, "yolo", "skip", f"{sym}: already positioned")
        return False
    if len(positions) >= config.YOLO["max_concurrent_positions"]:
        print(f"[yolo] reject {sym}: max concurrent positions "
              f"({config.YOLO['max_concurrent_positions']})")
        db.log_event(ACCOUNT, "yolo", "no_go", f"{sym}: max concurrent positions")
        return False

    side = str(a.get("side", "")).lower()
    if side not in ("buy", "sell"):
        print(f"[yolo] reject {sym}: bad side {side!r}")
        db.log_event(ACCOUNT, "yolo", "no_go", f"{sym}: bad side {side!r}")
        return False
    long = side == "buy"

    ref = _ref_price(client, sym)
    if ref is None or ref <= 0:
        print(f"[yolo] reject {sym}: no reference price")
        db.log_event(ACCOUNT, "yolo", "no_go", f"{sym}: no reference price")
        return False

    size_pct = _clamp(float(a.get("size_pct", 0.05) or 0.05), 0.0,
                      config.YOLO["max_position_pct"])
    notional = equity * size_pct
    qty = max(1, int(notional / ref))

    # Alpaca validates bracket legs against the LIVE price at submission and
    # rejects any stop within $0.01 of it (HTTP 422, code 42210000 — seen on
    # MSFT 2026-09-08). Enforce a small % buffer so a stale/gap/tight stop can
    # never 422 again; violations are logged no_go (fail-closed, no retry).
    min_stop = max(0.01, ref * config.YOLO["min_stop_dist_pct"])
    min_tgt = max(0.01, ref * config.YOLO["min_target_dist_pct"])

    stop = _to_price(a.get("stop"), ref)
    if stop is None or stop <= 0:
        print(f"[yolo] reject {sym}: missing/invalid stop-loss")
        db.log_event(ACCOUNT, "yolo", "no_go", f"{sym}: missing/invalid stop-loss")
        return False
    if (long and stop >= ref) or ((not long) and stop <= ref):
        print(f"[yolo] reject {sym}: stop {stop:.2f} on wrong side of {ref:.2f}")
        db.log_event(ACCOUNT, "yolo", "no_go", f"{sym}: stop on wrong side of entry")
        return False
    if (long and stop > ref - min_stop) or ((not long) and stop < ref + min_stop):
        print(f"[yolo] reject {sym}: stop {stop:.2f} within {min_stop:.2f} of live {ref:.2f}")
        db.log_event(ACCOUNT, "yolo", "no_go",
                     f"{sym}: stop {stop:.2f} too close to live price {ref:.2f}")
        return False

    target = _to_price(a.get("target"), ref)
    risk_dist = abs(ref - stop)
    if target is None or target <= 0:
        target = ref + 2 * risk_dist if long else ref - 2 * risk_dist
    if (long and target <= ref) or ((not long) and target >= ref):
        print(f"[yolo] reject {sym}: target on wrong side")
        db.log_event(ACCOUNT, "yolo", "no_go", f"{sym}: target on wrong side")
        return False
    if (long and target < ref + min_tgt) or ((not long) and target > ref - min_tgt):
        print(f"[yolo] reject {sym}: target {target:.2f} within {min_tgt:.2f} of live {ref:.2f}")
        db.log_event(ACCOUNT, "yolo", "no_go",
                     f"{sym}: target {target:.2f} too close to live price {ref:.2f}")
        return False

    max_risk = equity * config.YOLO["max_risk_pct"]
    if risk_dist * qty > max_risk:
        qty = max(1, int(max_risk / risk_dist))
    if qty * ref > buying_power:
        qty = max(1, int(buying_power / ref))
    if qty < 1:
        print(f"[yolo] reject {sym}: zero size")
        db.log_event(ACCOUNT, "yolo", "no_go", f"{sym}: zero size")
        return False

    print(f"[yolo] OPEN {side} {sym} x{qty} ref={ref:.2f} stop={stop:.2f} "
          f"target={target:.2f} notional~${qty * ref:,.0f}")
    if dry_run:
        return True

    try:
        order = client.bracket_order(sym, qty, side, stop_price=stop, target_price=target)
    except AlpacaError as e:
        print(f"[yolo] order error {sym}: {e}")
        db.log_event(ACCOUNT, "yolo", "error", f"{sym}: order error: {e}")
        return False

    db.insert_trade(
        account=ACCOUNT, strategy="yolo", symbol=sym,
        asset_class="etf" if sym in ETF_SYMBOLS else "equity",
        side="long" if long else "short", qty=qty,
        entry_price=ref, status="open", rr_planned=None,
        stop_price=stop, target_price=target,
        order_id=order.get("id"), client_order_id=order.get("client_order_id"),
        note=str(a.get("rationale", ""))[:200] or "yolo open",
        decision_json=json.dumps({
            "decision": "open",
            "rationale": str(a.get("rationale", "")),
            "action": {k: a.get(k) for k in ("symbol", "side", "size_pct", "stop", "target")},
            "context": {"equity": round(equity, 2),
                        "concurrent_positions": len(positions),
                        "max_risk_pct": config.YOLO["max_risk_pct"]},
        }),
    )
    print(f"[yolo] placed order {order.get('id')}")
    db.log_event(ACCOUNT, "yolo", "go", f"open {side} {sym} x{qty}",
                 detail=(f"ref={ref:.2f} stop={stop:.2f} target={target:.2f} | "
                         f"{str(a.get('rationale', ''))[:200]}"))
    return True


def _close_action(client, a, positions, dry_run) -> bool:
    sym = str(a.get("symbol", "")).strip().upper()
    if sym not in positions:
        return False
    print(f"[yolo] CLOSE {sym}")
    db.log_event(ACCOUNT, "yolo", "exit", f"close {sym}")
    if dry_run:
        return True
    try:
        client.close_position(sym)
    except AlpacaError as e:
        print(f"[yolo] close error {sym}: {e}")
        db.log_event(ACCOUNT, "yolo", "error", f"{sym}: close error: {e}")
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init_db()
    client = AlpacaClient(ACCOUNT)
    try:
        clock = client.clock()
    except AlpacaError as e:
        print(f"[yolo] clock error: {e}")
        db.log_event(ACCOUNT, "yolo", "error", f"clock error: {e}", dedup=True)
        return
    if not clock.get("is_open"):
        print("[yolo] market closed")
        db.log_event(ACCOUNT, "yolo", "skip", "market closed", dedup=True)
        return

    try:
        acct = client.account()
    except AlpacaError as e:
        print(f"[yolo] account error: {e}")
        db.log_event(ACCOUNT, "yolo", "error", f"account error: {e}", dedup=True)
        return
    equity = float(acct.get("equity", 0.0) or 0.0)
    buying_power = float(acct.get("buying_power", 0.0) or 0.0)
    if equity <= 0:
        print("[yolo] zero equity")
        db.log_event(ACCOUNT, "yolo", "error", "zero equity")
        return

    positions = {p["symbol"]: p for p in client.positions()}

    watch: dict = {}
    for sym in config.YOLO["watchlist"]:
        try:
            bars = client.bars(sym, timeframe="1Day", limit=30, start=None).get("bars", [])
        except AlpacaError:
            continue
        if not bars:
            continue
        closes = [float(b["c"]) for b in bars]
        # Show the LLM BOTH the live print and the last daily close so it can
        # see overnight gaps itself (live 490.56 vs last_close 510.12) instead
        # of sizing stops off a stale level.
        lt = client.latest_trade(sym)
        watch[sym] = {
            "last": round(lt["price"], 2) if lt else round(closes[-1], 2),
            "last_close": round(closes[-1], 2),
            "chg_5d_pct": round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None,
            "chg_30d_pct": round((closes[-1] / closes[0] - 1) * 100, 2),
            "high_30d": round(max(float(b["h"]) for b in bars), 2),
            "low_30d": round(min(float(b["l"]) for b in bars), 2),
        }

    open_map = {t["symbol"]: t for t in db.open_trades(ACCOUNT)}
    pos_ctx = [
        {
            "symbol": sym,
            "side": "long" if float(p["qty"]) > 0 else "short",
            "qty": abs(float(p["qty"])),
            "avg_entry": float(p["avg_entry_price"]),
            "market_value": float(p["market_value"]),
            "unrealized_pnl": float(p["unrealized_pl"]),
            "entry_rationale": ((open_map[sym]["note"] or "") if sym in open_map else "")[:160],
        }
        for sym, p in positions.items()
    ]

    closed = [t for t in db.all_trades(ACCOUNT) if t["status"] == "closed"]
    perf = {
        "n": len(closed),
        "net": round(sum(t["net_pnl"] or 0 for t in closed), 2),
        "win_rate": round(sum(1 for t in closed if (t["net_pnl"] or 0) > 0) / len(closed), 3)
        if closed else 0.0,
    }
    recent_trades = [
        {"symbol": t["symbol"], "side": t["side"], "net_pnl": t["net_pnl"],
         "note": (t["note"] or "")[:120]}
        for t in closed[-10:]
    ]
    lessons = ""
    if config.LESSONS_PATH.exists():
        lessons = config.LESSONS_PATH.read_text()[-1500:]

    system = (
        "You are the YOLO autonomous portfolio manager for a $10k Alpaca PAPER "
        "account. You have WIDE freedom: trade ANY retail US equity or ETF (the "
        "suggested_universe is only a starting point), any direction, any size "
        "within the safety floor. Always attach a stop-loss. Prefer 2:1 "
        "reward:risk but do not over-constrain yourself. LEARN from your own "
        "recent_trades and lessons_learned: avoid repeating mistakes, reinforce "
        "what has worked. You may open, close, or hold. Respond with ONLY a JSON "
        "object (no markdown):\n"
        '{"actions":[{"action":"open|close|hold","symbol":"SPY","side":"buy|sell",'
        '"size_pct":0.05,"stop":"-1.5%","target":"+3%"}],"rationale":"<one sentence>"}'
    )
    user = json.dumps({
        "equity": round(equity, 2),
        "buying_power": round(buying_power, 2),
        "positions": pos_ctx,
        "suggested_universe": watch,
        "recent_performance": perf,
        "recent_trades": recent_trades,
        "lessons_learned": lessons,
        "safety_floor": {
            "max_position_pct": config.YOLO["max_position_pct"],
            "max_risk_pct": config.YOLO["max_risk_pct"],
            "max_concurrent_positions": config.YOLO["max_concurrent_positions"],
        },
    }, indent=2)

    try:
        content = advisor.chat(system, user, temperature=0.3, max_tokens=4000)
    except Exception as e:
        print(f"[yolo] LLM error: {e}")
        db.log_event(ACCOUNT, "yolo", "error", f"LLM error: {e}")
        return

    plan = advisor._extract_json(content)
    if not isinstance(plan, dict):
        print("[yolo] unparseable plan (stand down)")
        db.log_event(ACCOUNT, "yolo", "error", "unparseable LLM plan")
        return
    actions = plan.get("actions") or []
    rationale = str(plan.get("rationale", ""))[:200]
    if not actions:
        print("[yolo] no actions proposed (stand down)")
        db.log_event(ACCOUNT, "yolo", "skip", "LLM proposed no actions", dedup=True)
        return

    executed = 0
    for a in actions:
        if not isinstance(a, dict):
            continue
        action = str(a.get("action", "")).strip().lower()
        try:
            if action == "close":
                ok = _close_action(client, a, positions, args.dry_run)
            elif action == "open":
                ok = _open_action(client, a, equity, buying_power, positions, args.dry_run)
            else:
                ok = False
        except Exception as e:
            print(f"[yolo] action error: {e}")
            ok = False
        if ok:
            executed += 1

    print(f"[yolo] executed {executed}/{len(actions)} actions | {rationale or '—'}")


if __name__ == "__main__":
    main()
