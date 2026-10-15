"""Weekly review probe #2 (2026-10-12) — READ ONLY.

1. NVDA: was the -1.26R a gap through the stop or intraday slippage?
2. Exact day-end equity per account (last snapshot of each day).

(The x_copy audit this probe used to start with was removed on 2026-10-15
together with the sleeve; the x_posts table no longer exists.)
"""
import sqlite3
from datetime import datetime, timezone

import config
from alpaca_rest import AlpacaClient

con = sqlite3.connect(config.DB_PATH)
con.row_factory = sqlite3.Row

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
