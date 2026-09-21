"""Tests for selfheal's R2 protection pass — the "naked at the exit" race.

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_selfheal.py

Plain asserts, no pytest (the container has no third-party packages).

The incident this pins (2026-09-21): an exit path releases a position's
protective legs and then submits an unconditional market close, which the broker
takes minutes to fill. On the next 10-minute tick the position looked NAKED, so
R2 tried to re-attach its stop and the broker rejected it with
HTTP 403 40310000 ('insufficient qty available', shares held for the close
order) — a false alarm logged as a hard error, twice, for daily XLF and yolo XLV.
R2 must leave such a position alone (and must NOT stop healing genuinely naked
ones, nor mistake an opening order for a closing one).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import db                                              # noqa: E402
import selfheal                                        # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


POSITIONS: dict = {}
ORDERS: dict = {}
TRADES: dict = {}
EVENTS: list = []
SUBMITTED: list = []


class FakeClient:
    """Minimal stand-in for AlpacaClient — records what selfheal tried to do."""

    def __init__(self, account: str):
        self.account = account
        self.positions_data = POSITIONS.get(account, [])
        self.orders_data = ORDERS.get(account, [])

    def clock(self):
        return {"is_open": True}

    def positions(self):
        return self.positions_data

    def orders_all(self, limit: int = 200):
        return self.orders_data

    def submit_order(self, order: dict):
        SUBMITTED.append(order)
        return {"id": "submitted-1"}

    def cancel_order(self, order_id: str):
        return {}

    def order(self, order_id: str):
        return {"status": "filled", "filled_qty": "1"}


def _install(account: str, positions: list, orders: list, trades: list) -> None:
    POSITIONS.clear()
    ORDERS.clear()
    TRADES.clear()
    EVENTS.clear()
    SUBMITTED.clear()
    POSITIONS[account] = positions
    ORDERS[account] = orders
    TRADES[account] = trades

    selfheal.AlpacaClient = FakeClient
    selfheal.config.SELFHEAL = {"enabled": True, "adopt_missing_rows": True,
                                "reattach_protection": True, "cancel_debris": True,
                                "void_unfilled": True, "open_tif": "day",
                                "after_hours_tif": "gtc"}
    db.open_trades = lambda acct=None: TRADES.get(acct, [])
    db.insert_trade = lambda **kw: (TRADES.setdefault(kw.get("account"), [])
                                    .append({"id": 99, **kw}) or 99)
    db.update_trade = lambda trade_id, **kw: None
    db.log_event = lambda acct, strategy, decision, reason="", detail=None, dedup=False: (
        EVENTS.append({"account": acct, "strategy": strategy, "decision": decision,
                       "reason": reason, "detail": detail}) or len(EVENTS))


def _pos(symbol: str, qty: float, avg: float = 55.0) -> dict:
    return {"symbol": symbol, "qty": str(qty), "avg_entry_price": str(avg),
            "asset_id": "asset-1"}


def _order(symbol: str, side: str, otype: str, qty: int = 35,
           status: str = "accepted", oid: str = "263d0e82-7cdd-4b8a-9a3b-361b6075d6fc") -> dict:
    return {"symbol": symbol, "side": side, "type": otype, "qty": str(qty),
            "status": status, "id": oid, "stop_price": None, "limit_price": None}


def _trade(symbol: str, qty: int, stop: float = 54.11) -> dict:
    return {"id": 13, "account": "daily", "symbol": symbol, "qty": qty,
            "stop_price": stop, "target_price": 58.72, "order_id": "41865bab",
            "note": "", "status": "open"}


def _decisions() -> list:
    return [e["decision"] for e in EVENTS]


def test_exit_in_flight_is_left_alone() -> None:
    print("R2: long position with its own market close still in flight (the 2026-09-21 race)")
    _install("daily", [_pos("XLF", 35)], [_order("XLF", "sell", "market")],
             [_trade("XLF", 35)])
    selfheal.selfheal_account("daily")
    check("no order was submitted", SUBMITTED == [], f"submitted={SUBMITTED}")
    check("journaled as no-action, not an error", _decisions() == ["skip"],
          f"decisions={_decisions()}")
    check("reason names the symbol", any("XLF" in e["reason"] for e in EVENTS),
          f"events={[e['reason'] for e in EVENTS]}")
    check("no error event", "error" not in _decisions())


def test_short_position_close_in_flight() -> None:
    print("R2: short position with its own buy-to-close in flight")
    _install("yolo", [_pos("XLU", -16)], [_order("XLU", "buy", "market", qty=16, oid="3567b902")],
             [_trade("XLU", 16)])
    selfheal.selfheal_account("yolo")
    check("no order was submitted", SUBMITTED == [], f"submitted={SUBMITTED}")
    check("journaled as no-action", _decisions() == ["skip"], f"decisions={_decisions()}")


def test_naked_position_is_still_healed() -> None:
    print("R2: genuinely naked long position, nothing pending -> stop must be re-attached")
    _install("daily", [_pos("XLF", 35)], [], [_trade("XLF", 35)])
    selfheal.selfheal_account("daily")
    stops = [o for o in SUBMITTED if o.get("type") == "stop"]
    check("one stop submitted", len(stops) == 1, f"submitted={SUBMITTED}")
    if stops:
        check("stop price copied verbatim from the trade row",
              stops[0]["stop_price"] == "54.11" and stops[0]["side"] == "sell",
              f"order={stops[0]}")
    check("journaled as fixed", "fix" in _decisions(), f"decisions={_decisions()}")


def test_opening_order_does_not_suppress_healing() -> None:
    print("R2: a pending order on the position's OWN side is not an exit -> heal anyway")
    _install("daily", [_pos("XLF", 35)], [_order("XLF", "buy", "market", qty=5, oid="beef0001")],
             [_trade("XLF", 35)])
    selfheal.selfheal_account("daily")
    check("stop still submitted",
          [o for o in SUBMITTED if o.get("type") == "stop"] != [], f"submitted={SUBMITTED}")
    check("no skip event", "skip" not in _decisions(), f"decisions={_decisions()}")


def test_protected_position_untouched() -> None:
    print("R2: position whose 'held' bracket stop is live -> nothing to do")
    _install("yolo", [_pos("QQQ", 1, avg=728.32)],
             [_order("QQQ", "sell", "stop", qty=1, status="held", oid="4b4147b7")],
             [_trade("QQQ", 1, stop=713.90)])
    selfheal.selfheal_account("yolo")
    check("no order submitted", SUBMITTED == [], f"submitted={SUBMITTED}")
    check("nothing journaled", EVENTS == [], f"events={EVENTS}")


def main() -> None:
    for fn in (test_exit_in_flight_is_left_alone,
               test_short_position_close_in_flight,
               test_naked_position_is_still_healed,
               test_opening_order_does_not_suppress_healing,
               test_protected_position_untouched):
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        sys.exit(1)
    print("all selfheal checks passed")


if __name__ == "__main__":
    main()
