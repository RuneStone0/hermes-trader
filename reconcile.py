"""Reconcile trades.db against Alpaca FILL activities — source of truth for P/L.

For each account: pull recent fills, update open trades' entry to the actual
average fill price, and close any trade whose position has been fully exited
(computing gross P/L, modelled regulatory fees, and net P/L).

Run: python3 reconcile.py [account]   (no arg = all three accounts)
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime

import config
import db
import fees
from alpaca_rest import AlpacaClient, AlpacaError


def _is_option(symbol: str) -> bool:
    # OCC option symbols: [root][6-digit YYMMDD][C/P][8-digit strike]
    return len(symbol) >= 21 and symbol[-9] in "CP" and symbol[-8:].isdigit()


def _mult(symbol: str) -> int:
    return 100 if _is_option(symbol) else 1


def _ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _classify_close(client: AlpacaClient, closing_fills: list,
                    trade=None, exit_px: float | None = None) -> str | None:
    """Why a position closed, from the broker's own closing order type:
    stop/stop_limit -> 'stop', limit -> 'target', market -> 'market', else 'other'.

    Falls back to a PRICE-PROXIMITY heuristic against the trade's own
    stop_price/target_price when the order lookup cannot resolve (5 of the 7
    YOLO losers in the first two weeks logged close_reason=NULL even though
    every one of them exited within a cent of its stop — the dashboard then
    showed a red trade with no reason, which is exactly the "why did this
    close?" information the journal exists to carry).
    One order lookup per closing fill — cheap and only on close.
    """
    for f in closing_fills:
        oid = f.get("order_id")
        if not oid:
            continue
        try:
            o = client.order(oid)
        except AlpacaError:
            continue
        t = (o.get("type") or "").lower()
        if t in ("stop", "stop_limit"):
            return "stop"
        if t == "limit":
            return "target"
        if t == "market":
            return "market"
    return _classify_by_price(trade, exit_px)


def _classify_by_price(trade, exit_px: float | None,
                       tol_pct: float = 0.004) -> str | None:
    """Proximity fallback: did the exit land on the plan's stop or target?

    Deterministic and honest: 0.4% tolerance covers normal stop slippage and a
    small gap without mistaking a discretionary exit for a stop fill.
    """
    if trade is None or exit_px is None:
        return None
    try:
        stop = float(trade["stop_price"]) if trade["stop_price"] is not None else None
        target = float(trade["target_price"]) if trade["target_price"] is not None else None
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    for level, label in ((stop, "stop"), (target, "target")):
        if level and level > 0 and abs(exit_px - level) / level <= tol_pct:
            return label
    return "other"


def reconcile_account(account: str) -> None:
    client = AlpacaClient(account)
    # Snapshot equity/cash/last_equity for the dashboard's size-% column and
    # Portfolio value card (cheap, 10-min cadence, no broker call per page view).
    try:
        acct = client.account()
        equity = float(acct.get("equity") or 0.0)
        cash = float(acct.get("cash") or 0.0)
        db.save_account_state(
            account,
            equity=equity,
            cash=cash,
            last_equity=float(acct.get("last_equity") or 0.0) or None,
            starting_equity=config.STARTING_CAPITAL.get(account),  # set-once baseline
        )
        # Time series: without it there is no high-water mark, so no drawdown to
        # de-risk on and no equity curve for the dashboard (see risk_gov.py).
        db.record_equity(account, equity, cash)
    except (AlpacaError, TypeError, ValueError):
        pass

    positions = {p["symbol"]: p for p in client.positions()}

    fills = client._request("GET", "/v2/account/activities?activity_types=FILL&page_size=100&direction=desc")
    fills_by_symbol: dict[str, list] = defaultdict(list)
    for f in fills:
        fills_by_symbol[f["symbol"]].append(f)

    for t in db.open_trades(account):
        sym = t["symbol"]
        if sym in positions:
            # Still open -> refresh actual entry price + latest mark & unrealized
            # P/L (the broker reports both on the position object, so it's free).
            p = positions[sym]
            avg = float(p["avg_entry_price"])
            last, upl = p.get("current_price"), p.get("unrealized_pl")
            db.update_trade(
                t["id"], entry_price=round(avg, 4),
                last_price=(round(float(last), 4) if last not in (None, "") else None),
                unrealized_pl=(round(float(upl), 2) if upl not in (None, "") else None),
            )
            continue

        # position fully exited -> compute exit from closing fills
        entry_ts = _ts(t["entry_time"] or t["created_at"])
        opposite = "sell" if t["side"] == "long" else "buy"
        closing = [f for f in fills_by_symbol.get(sym, []) if f["side"] == opposite]
        if entry_ts:
            closing = [f for f in closing if (_ts(f.get("transaction_time")) or entry_ts) >= entry_ts]

        if not closing:
            continue  # can't determine exit yet

        tot_qty = sum(abs(float(f["qty"])) for f in closing)
        if tot_qty <= 0:
            continue
        exit_px = sum(float(f["price"]) * abs(float(f["qty"])) for f in closing) / tot_qty
        entry_px = float(t["entry_price"] or 0.0)
        qty = float(t["qty"])
        mult = _mult(sym)

        gross = (exit_px - entry_px) * qty * mult if t["side"] == "long" else (entry_px - exit_px) * qty * mult
        f = fees.round_trip_equity_fees(qty, buy_price=entry_px, sell_price=exit_px)
        db.update_trade(
            t["id"], exit_price=round(exit_px, 4), status="closed",
            gross_pnl=round(gross, 2), fees=f, net_pnl=round(gross - f, 2),
            exit_time=closing[0].get("transaction_time"),
            close_reason=_classify_close(client, closing, t, exit_px),
        )
        print(f"[{account}] closed {sym} {t['side']} x{qty:g}: "
              f"gross ${gross:.2f} fees ${f:.2f} net ${gross-f:.2f}")


def save_benchmark(client: AlpacaClient, symbol: str = "SPY", days: int = 180) -> None:
    """Cache the benchmark's daily closes so account pages can plot buy-and-hold
    next to the bot's own equity curve. Called once per reconcile pass (not per
    account) — three identical SPY fetches would be pure waste."""
    try:
        bars = client.bars(symbol, timeframe="1Day", limit=days, start=None).get("bars", [])
    except AlpacaError:
        return
    db.save_benchmark(symbol, [(str(b["t"])[:10], float(b["c"])) for b in bars])


def main() -> None:
    db.init_db()
    accounts = [sys.argv[1]] if len(sys.argv) > 1 else ["daily", "weekly", "yolo"]
    for a in accounts:
        try:
            reconcile_account(a)
        except AlpacaError as e:
            print(f"[{a}] error: {e}")
    try:
        save_benchmark(AlpacaClient(accounts[0]))
    except AlpacaError as e:
        print(f"[benchmark] error: {e}")


if __name__ == "__main__":
    main()
