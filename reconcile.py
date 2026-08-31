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


def reconcile_account(account: str) -> None:
    client = AlpacaClient(account)
    positions = {p["symbol"]: p for p in client.positions()}

    fills = client._request("GET", "/v2/account/activities?activity_types=FILL&page_size=100&direction=desc")
    fills_by_symbol: dict[str, list] = defaultdict(list)
    for f in fills:
        fills_by_symbol[f["symbol"]].append(f)

    for t in db.open_trades(account):
        sym = t["symbol"]
        if sym in positions:
            # still open -> refresh actual entry price
            avg = float(positions[sym]["avg_entry_price"])
            db.update_trade(t["id"], entry_price=round(avg, 4))
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
        )
        print(f"[{account}] closed {sym} {t['side']} x{qty:g}: "
              f"gross ${gross:.2f} fees ${f:.2f} net ${gross-f:.2f}")


def main() -> None:
    db.init_db()
    accounts = [sys.argv[1]] if len(sys.argv) > 1 else ["daily", "weekly", "yolo"]
    for a in accounts:
        try:
            reconcile_account(a)
        except AlpacaError as e:
            print(f"[{a}] error: {e}")


if __name__ == "__main__":
    main()
