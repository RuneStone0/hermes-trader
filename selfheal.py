"""selfheal.py — deterministic broker-state self-healing (no LLM, no Hermes).

Runs every 10 minutes inside the container (see app.py JOBS). It never opens
NEW trades and never invents risk levels; it only restores the INTENDED state
when a runtime anomaly breaks it. Every action is journaled to the events
table (strategy='selfheal') so the dashboard, reconcile, and the Hermes
watchdog all see an audit trail.

Rules (each individually toggled in config.SELFHEAL):
  R1 adopt    — broker position with no open trade row -> insert one from the
                broker's own data (avg_entry/qty), warn (no stop known yet).
                If the DB row exists but qty drifted, sync it.
  R2 protect  — open position with NO protective stop order open (e.g. day-TIF
                bracket legs cancelled at 20:00 ET) -> re-place stop + target
                legs, prices copied VERBATIM from the trade's stop_price /
                target_price. If no stop_price is on record -> log error and
                leave it for the Hermes watchdog (never invent a level).
  R3 debris   — open protective orders (stops/limits) on symbols with NO
                position and no pending entry order -> cancel (stale bracket
                siblings that could misfire on a future move).
  R4 void     — DB trade rows whose broker order was cancelled with zero fills
                -> close the row as void (net 0) so the books stop lying.

Market-open gating: order placement uses TIF 'day' while the market is open
and 'gtc' after the close (so a position that legitimately survives overnight
keeps its stop — the user's hard 'always a stop-loss' rule).

Usage: python3 selfheal.py [account] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys

import config
import db
from alpaca_rest import AlpacaClient, AlpacaError

# Alpaca statuses that mean "this order is still live". NOTE: ?status=open
# silently omits 'held' bracket legs, so scans must use orders_all() + this set.
OPEN_STATUSES = {
    "new", "accepted", "held", "partially_filled",
    "pending_new", "pending_cancel", "pending_replace",
}
# An order can only PROTECT a position if a fill would close it (stop/stop_limit
# on the opposite side). A bare take-profit limit does NOT protect.
PROTECTIVE_TYPES = {"stop", "stop_limit"}
# An order that could OPEN a position if filled (pending entry order).
ENTRY_TYPES = {"market", "limit"}
# Common ETFs — for the db asset_class label of adopted rows (same set as
# yolo_run). Everything else is labelled equity.
_ETF_SYMBOLS = {
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "IEF", "HYG",
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "SMH",
}


def _side_of(qty: float) -> str:
    return "long" if qty > 0 else "short"


def _opposite_side(side: str) -> str:
    return "sell" if side == "long" else "buy"


def _log(account: str, decision: str, reason: str, detail: str | None = None,
         dedup: bool = False) -> None:
    print(f"[selfheal] {account}: {decision}: {reason}" + (f" | {detail}" if detail else ""))
    db.log_event(account, "selfheal", decision, reason, detail=detail, dedup=dedup)


def _has_protection(open_orders: list[dict], side: str) -> bool:
    """True if any open order would act as a protective stop for `side`."""
    want = "sell" if side == "long" else "buy"
    return any(o.get("side") == want and o.get("type") in PROTECTIVE_TYPES
               for o in open_orders)


def selfheal_account(account: str, dry_run: bool = False) -> None:
    if not config.SELFHEAL.get("enabled", True):
        return
    client = AlpacaClient(account)
    try:
        clock = client.clock()
    except AlpacaError as e:
        # Repeated broker-side clock failures are a no-op/waiting state, not a
        # new event per tick: dedup keeps one row (daily/weekly/yolo_run do the
        # same for clock errors) instead of 3 new error events every 10 min.
        _log(account, "error", f"clock unavailable: {e}", dedup=True)
        return
    market_open = bool(clock.get("is_open"))

    positions = {p["symbol"]: p for p in client.positions()}
    open_orders = [o for o in client.orders_all(limit=200)
                   if o.get("status") in OPEN_STATUSES]
    by_sym: dict[str, list[dict]] = {}
    for o in open_orders:
        by_sym.setdefault(o["symbol"], []).append(o)

    trades = {t["symbol"]: t for t in db.open_trades(account)}

    # ---- R1: adopt / sync DB rows for broker positions ------------------ #
    if config.SELFHEAL.get("adopt_missing_rows", True):
        for sym, p in positions.items():
            qty = float(p["qty"])
            side = _side_of(qty)
            t = trades.get(sym)
            if t is None:
                detail = (f"broker position {side} x{abs(qty):g} @ "
                          f"{float(p['avg_entry_price']):.2f} has no DB row")
                if dry_run:
                    print(f"[selfheal] {account}: R1 would adopt {sym} ({detail})")
                    continue
                db.insert_trade(
                    account=account, strategy="selfheal", symbol=sym,
                    asset_class="etf" if sym in _ETF_SYMBOLS else "equity",
                    side=side, qty=abs(qty),
                    entry_price=float(p["avg_entry_price"]), status="open",
                    order_id=p.get("asset_id"), note="selfheal: adopted from broker (missing DB row)",
                )
                _log(account, "warn", f"adopted {sym} from broker (missing DB row)", detail)
            elif abs(qty) != float(t["qty"]):
                if not dry_run:
                    db.update_trade(t["id"], qty=abs(qty))
                _log(account, "fix", f"synced {sym} qty {float(t['qty']):g} -> {abs(qty):g}")

    # ---- R2: re-attach protective stop/target for naked positions ------- #
    if config.SELFHEAL.get("reattach_protection", True):
        # Re-read after R1 so adopted rows are visible.
        trades = {t["symbol"]: t for t in db.open_trades(account)}
        for sym, p in positions.items():
            qty = abs(float(p["qty"]))
            side = _side_of(float(p["qty"]))
            if _has_protection(by_sym.get(sym, []), side):
                continue  # protected (bracket stop leg live, incl. 'held')
            t = trades.get(sym)
            stop_px = float(t["stop_price"]) if t and t["stop_price"] else None
            if stop_px is None or stop_px <= 0:
                _log(account, "error",
                     f"{sym} ({side} x{qty:g}) is NAKED and has no stop_price on "
                     f"record — cannot protect, needs human/LLM decision")
                continue
            tif = config.SELFHEAL.get("open_tif", "day") if market_open \
                else config.SELFHEAL.get("after_hours_tif", "gtc")
            tgt_px = float(t["target_price"]) if t and t["target_price"] else None
            opp = _opposite_side(side)
            detail = (f"re-attaching protective {opp} stop {stop_px:.2f}"
                      + (f" + target {tgt_px:.2f}" if tgt_px else "")
                      + f" (tif={tif}); prior bracket legs gone")
            if dry_run:
                print(f"[selfheal] {account}: R2 would protect {sym} ({detail})")
                continue
            try:
                client.submit_order({
                    "symbol": sym, "qty": str(int(qty)), "side": opp,
                    "type": "stop", "stop_price": f"{stop_px:.2f}",
                    "time_in_force": tif,
                })
            except AlpacaError as e:
                _log(account, "error", f"{sym}: protection re-attach failed: {e}")
                continue
            _log(account, "fix", f"{sym}: re-attached protective stop {stop_px:.2f}", detail)
            # The take-profit target is BEST-EFFORT: Alpaca lets only ONE
            # independent closing order hold the full position qty, so once the
            # protective stop above is accepted, a separate limit sell for the
            # same qty is rejected (403 "insufficient qty available"). The stop
            # is the safety-critical leg and is already live — log a warn, not
            # an error, so a benign target gap never masks the stop fix or
            # wakes the watchdog as a hard failure.
            if tgt_px and tgt_px > 0:
                try:
                    client.submit_order({
                        "symbol": sym, "qty": str(int(qty)), "side": opp,
                        "type": "limit", "limit_price": f"{tgt_px:.2f}",
                        "time_in_force": tif,
                    })
                except AlpacaError as e:
                    _log(account, "warn",
                         f"{sym}: take-profit target not re-attached "
                         f"(stop already protects {qty:g} shares): {e}")

    # ---- R3: cancel orphan protective orders on flat symbols ------------ #
    if config.SELFHEAL.get("cancel_debris", True):
        for sym, ol in by_sym.items():
            if sym in positions:
                continue
            # A pending ENTRY order (bracket waiting to fill) is not debris:
            # its legs ride with it. Only cancel when nothing can open here.
            if any(o.get("type") in ENTRY_TYPES for o in ol):
                continue
            for o in ol:
                if dry_run:
                    print(f"[selfheal] {account}: R3 would cancel orphan {sym} "
                          f"{o.get('type')} {o.get('side')} {o.get('id', '')[:8]}")
                    continue
                try:
                    client.cancel_order(o["id"])
                except AlpacaError as e:
                    _log(account, "error", f"{sym}: cancel debris failed: {e}")
                    continue
                _log(account, "fix", f"cancelled orphan {sym} {o.get('type')} {o.get('side')} "
                                    f"(no position, no pending entry)")

    # ---- R4: void DB rows whose order died unfilled --------------------- #
    if config.SELFHEAL.get("void_unfilled", True):
        for t in trades.values():
            if t["symbol"] in positions or t["symbol"] in by_sym:
                continue  # position or open orders exist — not void
            oid = t["order_id"]
            if not oid:
                continue
            try:
                o = client.order(oid)
            except AlpacaError:
                continue  # order not found (very old) — leave to reconcile
            if o.get("status") in ("canceled", "expired") and \
                    not float(o.get("filled_qty") or 0):
                if not dry_run:
                    db.update_trade(t["id"], status="closed", exit_price=None,
                                    gross_pnl=0.0, fees=0.0, net_pnl=0.0,
                                    note=(t["note"] or "") + " | selfheal: order cancelled unfilled (void)")
                _log(account, "fix", f"voided {t['symbol']} trade row "
                                     f"(order {oid[:8]} cancelled, 0 filled)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("account", nargs="?", default=None,
                    help="daily|weekly|yolo (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init_db()
    accounts = [args.account] if args.account else ["daily", "weekly", "yolo"]
    for a in accounts:
        try:
            selfheal_account(a, dry_run=args.dry_run)
        except AlpacaError as e:
            print(f"[selfheal] {a}: account error: {e}")
        except Exception as e:  # never let one account take down the run
            print(f"[selfheal] {a}: unexpected error: {e!r}")


if __name__ == "__main__":
    main()
