"""Read-only: post-exit run-on (the first 5 five-minute bars after the exit) in R.

Question it answers: "is the exit leaving money on the table?" A low capture can
mean the exit is too tight OR that the tape never offers more; the difference is
what price does AFTER the fill. For every closed trade: exit_time + first 5
5-min bars, converted to R with that trade's own entry-to-stop distance.

RAW bars only: `bars()` defaults to adjustment="all", and a dividend-adjusted
series is priced on a different basis than the raw fill (see
references/exit-quality-telemetry.md, trap 3).

    python3 /opt/data/profiles/trader/scripts/send_probe.py probes/post_exit_runon.py

No writes, no orders.
"""
import json, sqlite3, statistics as st
from datetime import datetime, timedelta, timezone

import config
import r_telemetry as rt
from alpaca_rest import AlpacaClient


def q(sql, args=()):
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _ts(s):
    return rt._ts(s)


cl = AlpacaClient("daily")
rows = q("SELECT * FROM trades WHERE status='closed' AND strategy='yolo' ORDER BY exit_time")
out = []
for t in rows:
    t1 = _ts(t["exit_time"])
    risk = rt.risk_per_share(t)
    if not t1 or not risk or t["exit_price"] is None:
        continue
    t2 = t1 + timedelta(days=1)
    bars = rt._bars_in_window(cl, t, t1 + timedelta(minutes=1), min(t2, datetime.now(timezone.utc)))
    bars = bars[:5]
    if not bars:
        out.append({"sym": t["symbol"], "exit": t["exit_time"][:10], "reason": t["close_reason"],
                    "mfe": t["mfe_r"], "runon": None})
        continue
    xp = float(t["exit_price"])
    if t["side"] == "long":
        run = (max(h for _, h, _ in bars) - xp) / risk
    else:
        run = (xp - min(l for _, _, l in bars)) / risk
    out.append({"sym": t["symbol"], "exit": t["exit_time"][:10], "reason": t["close_reason"],
                "mfe": t["mfe_r"], "runon": round(run, 3), "n": len(bars)})


def summ(label, rows):
    vals = [r["runon"] for r in rows if r["runon"] is not None]
    if not vals:
        print(f"  {label}: n=0 measurable")
        return
    print(f"  {label}: n={len(vals)} medRunOn={round(st.median(vals),2)}R "
          f"mean={round(sum(vals)/len(vals),2)}R pos_share={round(100*sum(1 for v in vals if v>0)/len(vals))}% "
          f"ge0.5R={sum(1 for v in vals if v>=0.5)} ge1R={sum(1 for v in vals if v>=1.0)}")


print("per-trade:")
for r in out:
    print(f"  {r['exit']} {r['sym']:5s} {str(r['reason']):7s} mfe {str(r['mfe']):>6s} runon {r['runon']}")
print("\nsummary:")
summ("ALL", out)
summ("exit=market", [r for r in out if r["reason"] == "market"])
summ("exit=target", [r for r in out if r["reason"] == "target"])
summ("exit=stop", [r for r in out if r["reason"] == "stop"])
summ("MFE>=1R cohort", [r for r in out if r["mfe"] is not None and float(r["mfe"]) >= 1.0])
summ("MFE<1R cohort", [r for r in out if r["mfe"] is not None and float(r["mfe"]) < 1.0])
print("\nmeasurable", sum(1 for r in out if r["runon"] is not None), "of", len(out))
