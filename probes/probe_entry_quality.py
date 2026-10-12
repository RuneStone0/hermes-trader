"""Weekly review probe #3 (2026-10-12) — READ ONLY.

A. Entry-quality measurement: for every closed trade with an entry date, compute
   the entry's position in its own 20-day range, its distance above the 20/50-day
   SMA, RSI14, and the ATR14 percentile at entry — then print them next to the
   trade's measured MFE (offered) and realized R. This is the measurement that
   attacks the standing open question 'is it the entries?'.

B. Protection audit: every open position must carry a live protective stop leg
   (orders_all(), because bracket legs sit in status 'held').
"""
from datetime import datetime, timezone

import config
import db
import indicators
from alpaca_rest import AlpacaClient

START = "2024-06-01T00:00:00Z"
_cache: dict[str, list] = {}


def daily(client, sym):
    if sym not in _cache:
        b = client.bars(sym, "1Day", start=START, limit=10000).get("bars") or []
        _cache[sym] = b
    return _cache[sym]


def feats(client, sym, entry_price, entry_date):
    bars = daily(client, sym)
    upto = [b for b in bars if (b.get("t") or "")[:10] <= entry_date]
    if len(upto) < 260:
        return None
    closes = [b["c"] for b in upto]
    highs = [b["h"] for b in upto]
    lows = [b["l"] for b in upto]
    s20 = indicators.sma(closes, 20)
    s50 = indicators.sma(closes, 50)
    a = indicators.atr(upto, 14)
    r14 = indicators.rsi(closes, 14)
    if not s20[-1] or not s50[-1] or not a[-1]:
        return None
    hi20, lo20 = max(highs[-20:]), min(lows[-20:])
    rng = (hi20 - lo20) or 1e-9
    # ATR percentile: rank today's ATR% against the trailing 252 sessions
    atrp = [x / c for x, c in zip(a[-252:], closes[-252:]) if x]
    cur = a[-1] / closes[-1]
    pctile = 100.0 * sum(1 for x in atrp if x <= cur) / len(atrp) if atrp else None
    return {
        "in_range": round((entry_price - lo20) / rng, 3),
        "vs_sma20": round(entry_price / s20[-1] - 1, 4),
        "vs_sma50": round(entry_price / s50[-1] - 1, 4),
        "rsi14": round(r14[-1], 1) if r14[-1] is not None else None,
        "atr_pctile": round(pctile, 1) if pctile is not None else None,
    }


def r_of(t):
    risk = abs(float(t["entry_price"]) - float(t["stop_price"])) if t["stop_price"] else None
    if not risk or t["net_pnl"] is None or not t["qty"]:
        return None
    return float(t["net_pnl"]) / (risk * abs(float(t["qty"])))


con = db.connect() if hasattr(db, "connect") else None
import sqlite3
con = sqlite3.connect(config.DB_PATH)
con.row_factory = sqlite3.Row
rows = list(con.execute(
    "SELECT * FROM trades WHERE exit_time IS NOT NULL ORDER BY exit_time"))

clients: dict[str, AlpacaClient] = {}
print(f"{'closed':16} {'book':10} {'sym':6} {'R':>6} {'MFE':>6} {'inRng':>6} "
      f"{'vs20d':>7} {'vs50d':>7} {'rsi14':>6} {'ATLpct':>7}")
recs = []
for t in rows:
    c = clients.setdefault(t["account"], AlpacaClient(t["account"]))
    et = (t["entry_time"] or t["created_at"] or "")
    f = feats(c, t["symbol"], float(t["entry_price"]), et[:10])
    r = r_of(t)
    if f is None:
        print(f"{t['exit_time'][:16]:16} {t['strategy']:10} {t['symbol']:6} "
              f"{r if r is None else round(r,2):>6}  (no daily history)")
        continue
    recs.append(dict(sym=t["symbol"], book=t["strategy"], r=r if r else 0.0,
                     mfe=t["mfe_r"] or 0.0, **f))
    print(f"{t['exit_time'][:16]:16} {t['strategy']:10} {t['symbol']:6} "
          f"{r:>6.2f} {(t['mfe_r'] or 0):>6.2f} {f['in_range']:>6.2f} "
          f"{f['vs_sma20']*100:>6.2f}% {f['vs_sma50']*100:>6.2f}% "
          f"{f['rsi14']:>6} {f['atr_pctile']:>7}")

yolo = [x for x in recs if x["book"] == "yolo"]
if yolo:
    print("\n--- yolo: entry feature vs outcome (n=%d) ---" % len(yolo))
    hit = [x for x in yolo if x["mfe"] >= 1.0]
    miss = [x for x in yolo if x["mfe"] < 1.0]
    for label, grp in (("MFE>=1R", hit), ("MFE<1R", miss)):
        if not grp:
            continue
        n = len(grp)
        print(f"  {label:9} n={n:2}  medInRange={sorted(x['in_range'] for x in grp)[n//2]:.2f}  "
              f"medVs20d={sorted(x['vs_sma20'] for x in grp)[n//2]*100:+.2f}%  "
              f"medATLpct={sorted(x['atr_pctile'] for x in grp)[n//2]:.0f}")
    # crude split: entries at the top third of the range vs the rest
    top = [x for x in yolo if x["in_range"] >= 0.75]
    rest = [x for x in yolo if x["in_range"] < 0.75]
    for label, grp in (("in_range>=0.75", top), ("in_range<0.75", rest)):
        if grp:
            print(f"  {label:16} n={len(grp):2}  sumR={sum(x['r'] for x in grp):+.2f}  "
                  f"avgR={sum(x['r'] for x in grp)/len(grp):+.3f}  "
                  f"MFE>=1R {sum(1 for x in grp if x['mfe']>=1.0)}/{len(grp)}")

# ---------------------------------------------------------------- protection
print("\n=== protection audit (live broker) ===")
for acct in ("daily", "weekly", "yolo"):
    c = clients.setdefault(acct, AlpacaClient(acct))
    pos = c.positions() or []
    orders = c.orders_all(limit=500) or []
    print(f"-- {acct}: {len(pos)} position(s), {len(orders)} order(s) total")
    for p in pos:
        sym = p.get("symbol")
        side = p.get("side")
        qty = p.get("qty")
        legs = [o for o in orders if o.get("symbol") == sym
                and o.get("status") in ("new", "accepted", "held", "pending_new",
                                        "partially_filled")]
        stop = [o for o in legs if (o.get("type") or "").lower().startswith("stop")
                and o.get("side") != side]
        tgt = [o for o in legs if (o.get("type") or "").lower() in ("limit", "market")
               and o.get("side") != side]
        print(f"   {sym:6} {side:5} qty={qty}  stop_legs={[(o['type'], o.get('stop_price'), o['status']) for o in stop]}"
              f"  other_exit={[(o['type'], o.get('limit_price'), o['status']) for o in tgt]}")
    flat_orphans = [o for o in orders
                    if o.get("status") in ("new", "accepted", "held", "pending_new")
                    and o.get("symbol") not in {p.get("symbol") for p in pos}]
    if flat_orphans:
        print("   ORPHAN live orders on flat symbols:",
              [(o["symbol"], o["side"], o["type"], o["status"]) for o in flat_orphans])
