"""Weekly review probe #2 (2026-10-12) — READ ONLY.

1. x_copy audit: rows vs disposition journal lines, split at the bdef56d fix.
2. NVDA: was the -1.26R a gap through the stop or intraday slippage?
3. Exact day-end equity per account (last snapshot of each day).
"""
import sqlite3
from datetime import datetime, timezone

import config
from alpaca_rest import AlpacaClient

con = sqlite3.connect(config.DB_PATH)
con.row_factory = sqlite3.Row

print("=== x_posts rows (newest 12) ===")
for r in con.execute("SELECT post_id, state, kind, symbol, posted_at, seen_at FROM x_posts ORDER BY seen_at DESC LIMIT 12"):
    print(dict(r))

print("\n=== x_posts counts by state ===")
for r in con.execute("SELECT state, COUNT(*) n, MIN(seen_at) first, MAX(seen_at) last FROM x_posts GROUP BY state"):
    print(dict(r))
print("total rows:", list(con.execute("SELECT COUNT(*) FROM x_posts"))[0][0])

FIX = "2026-10-05T12:10"
print("\n=== after-fix audit (seen_at >= %s) ===" % FIX)
rows = list(con.execute("SELECT COUNT(*) FROM x_posts WHERE seen_at >= ?", (FIX,)))[0][0]
lines = list(con.execute(
    "SELECT decision, COUNT(*) n FROM events WHERE strategy='x_copy' "
    "AND decision IN ('read','review','reject') AND created_at >= ? "
    "GROUP BY decision", (FIX,)))
print("x_posts rows since fix:", rows)
print("disposition journal lines since fix:", lines,
      "sum:", sum(x['n'] for x in lines))

print("\n=== x_copy events by decision ===")
for r in con.execute("SELECT decision, COUNT(*) n, MIN(created_at) first, MAX(created_at) last FROM events WHERE strategy='x_copy' GROUP BY decision"):
    print(dict(r))

# ---------------- NVDA around the stop -------------------------------
print("\n=== NVDA 5-min bars 2026-10-07T18:00Z -> 2026-10-08T18:00Z ===")
c = AlpacaClient("yolo")
bars = c.bars("NVDA", "5Min", start="2026-10-07T18:00:00Z", limit=1000).get("bars") or []
print("5min bars returned:", len(bars))
for b in bars:
    print(b.get("t"), "O", b.get("o"), "H", b.get("h"), "L", b.get("l"), "C", b.get("c"))

print("\n=== NVDA daily bars 10-05..10-09 ===")
for b in (c.bars("NVDA", "1Day", start="2026-10-05T00:00:00Z", limit=20).get("bars") or []):
    print(b.get("t"), "O", b.get("o"), "H", b.get("h"), "L", b.get("l"), "C", b.get("c"))

# ---------------- exact day-end equity --------------------------------
print("\n=== day-end equity (last snapshot per account/day) ===")
for r in con.execute(
    "SELECT account, substr(ts,1,10) d, equity, ts FROM equity_history e "
    "WHERE ts = (SELECT MAX(ts) FROM equity_history x WHERE x.account=e.account "
    "            AND substr(x.ts,1,10)=substr(e.ts,1,10)) "
    "AND substr(ts,1,10) >= '2026-10-02' ORDER BY d, account"):
    print(dict(r))
