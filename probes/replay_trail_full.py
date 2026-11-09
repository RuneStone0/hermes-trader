"""Read-only: replay a FULL-POSITION trailing stop against the real closed book.

Question it answers: "would a mechanical trail beat the exits the bot actually
took?" This is NOT settled #19 (banking part of the position at a threshold,
which caps the runner); a trail on the full position only tightens the give-back.

Same entries, same 5-min raw bars, walked bar by bar. The trail level in force
during a bar is computed from the peak as of the PREVIOUS bar (no intra-bar
look-ahead); a bar touching both the original stop and the trail is booked at the
STOP (conservative); trades the trail never triggers keep the outcome they got.
Both intra-bar orderings are printed as a sensitivity check.

    python3 /opt/data/profiles/trader/scripts/send_probe.py probes/replay_trail_full.py

No writes, no orders. Result recorded in reports/exit_attribution_2026-11-09.md.
"""
import sqlite3, statistics as st
from datetime import datetime

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


cl = AlpacaClient("daily")
rows = q("SELECT * FROM trades WHERE status='closed' AND strategy='yolo' ORDER BY exit_time")

trades = []
for t in rows:
    t0 = rt.entry_ts(t)
    t1 = rt._ts(t["exit_time"])
    rps = rt.risk_per_share(t)
    if not t0 or not t1 or not rps or t["stop_price"] is None or t["target_price"] is None:
        continue
    bars = rt._bars_in_window(cl, t, t0, t1)
    if not bars:
        continue
    trades.append({"t": t, "bars": bars, "rps": rps, "long": t["side"] == "long",
                   "entry": float(t["entry_price"]), "stop": float(t["stop_price"]),
                   "target": float(t["target_price"]), "exit": float(t["exit_price"]),
                   "mfe": t["mfe_r"], "reason": t["close_reason"]})


def R_of(g, tr):
    d = (g - tr["entry"]) if tr["long"] else (tr["entry"] - g)
    return d / tr["rps"]


def replay(tr, frac, order="cons"):
    peak = tr["entry"]
    stop = tr["stop"]
    for _, h, l in tr["bars"]:
        if tr["long"]:
            if l <= stop:
                return R_of(stop, tr)
            lvl = max(stop, peak - frac * tr["rps"]) if frac is not None else None
            if order == "cons":
                if lvl is not None and lvl > stop and l <= lvl:
                    return R_of(lvl, tr)
                if h >= tr["target"]:
                    return R_of(tr["target"], tr)
            else:
                if h >= tr["target"]:
                    return R_of(tr["target"], tr)
                if lvl is not None and lvl > stop and l <= lvl:
                    return R_of(lvl, tr)
            peak = max(peak, h)
        else:
            if h >= stop:
                return R_of(stop, tr)
            lvl = min(stop, peak + frac * tr["rps"]) if frac is not None else None
            if order == "cons":
                if lvl is not None and lvl < stop and h >= lvl:
                    return R_of(lvl, tr)
                if l <= tr["target"]:
                    return R_of(tr["target"], tr)
            else:
                if l <= tr["target"]:
                    return R_of(tr["target"], tr)
                if lvl is not None and lvl < stop and h >= lvl:
                    return R_of(lvl, tr)
            peak = min(peak, l)
    return R_of(tr["exit"], tr)


base = sum(R_of(t["exit"], t) for t in trades)
print(f"replayable {len(trades)}/47 | baseline gross sumR {base:+.2f}")
print("\nfrac    CONS sumR (delta)      OPT sumR (delta)")
for frac in (0.25, 0.35, 0.5, 0.75, 1.0, 1.5):
    c = sum(replay(t, frac, "cons") for t in trades)
    o = sum(replay(t, frac, "opt") for t in trades)
    print(f"{frac:.2f}   {c:+7.2f} ({c-base:+6.2f})     {o:+7.2f} ({o-base:+6.2f})")

for order in ("cons", "opt"):
    best_f = max((0.25, 0.35, 0.5, 0.75, 1.0, 1.5),
                 key=lambda f: sum(replay(t, f, order) for t in trades))
    order_trades = sorted(trades, key=lambda t: R_of(t["exit"], t), reverse=True)
    kept = order_trades[2:]
    b = sum(R_of(t["exit"], t) for t in kept)
    alt = sum(replay(t, best_f, order) for t in kept)
    tot = sum(replay(t, best_f, order) for t in trades)
    better = sum(1 for t in trades if replay(t, best_f, order) - R_of(t["exit"], t) > 0.05)
    worse = sum(1 for t in trades if replay(t, best_f, order) - R_of(t["exit"], t) < -0.05)
    print(f"\n[{order}] best frac {best_f}: sumR {tot:+.2f} vs base {base:+.2f} (delta {tot-base:+.2f}); "
          f"better {better} worse {worse}")
    print(f"[{order}] drop-top-2: base {b:+.2f} -> alt {alt:+.2f} (delta {alt-b:+.2f})")
    sd = st.pstdev([replay(t, best_f, order) - R_of(t["exit"], t) for t in trades])
    ci = 1.96 * sd / (len(trades) ** 0.5)
    print(f"[{order}] per-trade delta sd {sd:.2f}R -> 95% CI half-width on the MEAN delta +/-{ci:.2f}R")
