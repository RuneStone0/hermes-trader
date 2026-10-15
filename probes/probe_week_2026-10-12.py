"""Weekly review probe 2026-10-12 — READ-ONLY except r_telemetry backfill."""
import sqlite3
from datetime import datetime, timedelta, timezone

import config

print("VERSION", config.VERSION)
print("MIN_CLOSED_TRADES_TO_TUNE", config.MIN_CLOSED_TRADES_TO_TUNE)

import r_telemetry
n = r_telemetry.backfill()
print("[telemetry] backfilled rows:", n)
print(r_telemetry.report())

con = sqlite3.connect(config.DB_PATH)
con.row_factory = sqlite3.Row

print("\n=== benchmark_history (SPY buy-and-hold) ===")
for r in con.execute("SELECT * FROM benchmark_history ORDER BY d"):
    print(dict(r))

print("\n=== equity_history last 12 days per account ===")
for r in con.execute(
        "SELECT account, substr(ts,1,10) d, MIN(equity) lo, MAX(equity) hi "
        "FROM equity_history WHERE ts >= '2026-09-28' GROUP BY account, d "
        "ORDER BY d, account"):
    print(dict(r))

print("\n=== closed trades, all ===")
for r in con.execute(
        "SELECT account, symbol, side, qty, entry_price, stop_price, exit_price,"
        " entry_time, exit_time, net_pnl, close_reason, mfe_r, mae_r, stop_atr"
        " FROM trades WHERE exit_time IS NOT NULL ORDER BY exit_time"):
    print(dict(r))

print("\n=== open trades ===")
for r in con.execute(
        "SELECT account, symbol, side, qty, entry_price, stop_price, target_price,"
        " entry_time, last_price, unrealized_pl, rr_planned FROM trades"
        " WHERE exit_time IS NULL"):
    print(dict(r))

print("\n=== events by strategy/decision (last 21 days) ===")
since = (datetime.now(timezone.utc) - timedelta(days=21)).strftime("%Y-%m-%d")
for r in con.execute(
        "SELECT strategy, decision, COUNT(*) n FROM events WHERE created_at >= ? "
        "GROUP BY strategy, decision ORDER BY n DESC", (since,)):
    print(dict(r))

print("\n=== error/warn events last 21 days ===")
for r in con.execute(
        "SELECT created_at, strategy, decision, substr(reason,1,200) reason FROM events "
        "WHERE created_at >= ? AND decision IN ('error','warn')"
        " ORDER BY created_at DESC LIMIT 40", (since,)):
    print(dict(r))

print("\n=== events, last 40 rows (all) ===")
for r in con.execute(
        "SELECT created_at, strategy, decision, substr(reason,1,140) reason FROM events"
        " ORDER BY id DESC LIMIT 40"):
    print(dict(r))
