"""Slot-cap fidelity + exit mix for the 20-day breakout rule (champion config)."""
import math
from collections import Counter, defaultdict

from alpaca_rest import AlpacaClient
from indicators import atr

UNIVERSE = ["SPY", "QQQ", "IWM", "GLD", "TLT", "XLE", "XLF", "XLV",
            "XLU", "XLP", "XLB", "XLK", "SMH", "SLV"]
START = "2020-07-27"
SLIP = 0.0001
P = {"n": 20, "stop_atr": 1.5, "tgt_atr": 4.0, "max_hold": 15}

client = AlpacaClient("weekly")
data = {}
for sym in UNIVERSE:
    resp = client.bars(sym, "1Day", limit=2000, start=START)
    data[sym] = [{"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "t": b["t"][:10]}
                 for b in (resp.get("bars") or [])]

# global calendar
dates = sorted({b["t"] for b in data["SPY"]})
dset = {s: {b["t"]: i for i, b in enumerate(data[s])} for s in UNIVERSE}
atrs = {s: atr(data[s], 14) for s in UNIVERSE}


def mean_ci(xs):
    n = len(xs)
    if n < 2:
        return (sum(xs) / n if xs else 0.0, 0.0)
    m = sum(xs) / n
    sd = (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
    return (m, 1.96 * sd / math.sqrt(n))


def signals_on(d):
    out = []
    for s in UNIVERSE:
        i = dset[s].get(d)
        if i is None or i < P["n"] + 20 or i + 1 >= len(data[s]):
            continue
        a = atrs[s][i]
        if a is None or a <= 0:
            continue
        hi = max(b["h"] for b in data[s][i - P["n"]:i])
        if data[s][i]["c"] > hi:
            out.append((s, i, a))
    return out


# cluster rate
cnt = Counter()
for d in dates:
    cnt[len(signals_on(d))] += 1
print("signals-per-day distribution (days with >=1 signal only):")
tot_days = sum(v for k, v in cnt.items() if k > 0)
for k in sorted(cnt):
    if k == 0:
        continue
    print("   %d signal(s): %d days (%.1f%%)" % (k, cnt[k], 100.0 * cnt[k] / tot_days))
print("   zero-signal days:", cnt[0], "of", len(dates))


def run(cap):
    open_until = {}          # symbol -> exit date index
    trades = []
    for d in dates:
        sigs = signals_on(d)
        if not sigs:
            continue
        free = cap - len(open_until)
        for s, i, a in sigs:
            if s in open_until:
                continue
            if free <= 0:
                continue
            free -= 1
            entry = data[s][i + 1]["o"] * (1 + SLIP)
            stop = entry - P["stop_atr"] * a
            tgt = entry + P["tgt_atr"] * a
            if stop <= 0 or entry <= stop:
                continue
            last = min(len(data[s]) - 1, i + 1 + P["max_hold"])
            exit_px, why, j = None, None, last
            for k in range(i + 1, last + 1):
                if data[s][k]["l"] <= stop:
                    exit_px, why, j = stop, "stop", k
                    break
                if data[s][k]["h"] >= tgt:
                    exit_px, why, j = tgt, "target", k
                    break
            if exit_px is None:
                exit_px, why = data[s][last]["c"], "time"
            r = (exit_px * (1 - SLIP) - entry) / (entry - stop)
            trades.append({"sym": s, "d": data[s][i + 1]["t"], "R": r, "why": why})
            open_until[s] = data[s][j]["t"]
        # release slots whose exit date has passed
        open_until = {s: x for s, x in open_until.items() if x > d}
    return trades


for cap in (99, 6, 4, 3):
    trades = run(cap)
    print("\n== slot cap %s: n=%d ==" % (cap if cap < 99 else "none", len(trades)))
    for name, lo, hi in (("2020H2-22", "2020-07-27", "2022-12-31"), ("2023-24", "2023-01-01", "2024-12-31"),
                         ("2025", "2025-01-01", "2025-12-31"), ("2026", "2026-01-01", "2026-12-31"),
                         ("since Sep", "2026-09-01", "2026-12-31")):
        xs = [t["R"] for t in trades if lo <= t["d"] <= hi]
        m, ci = mean_ci(xs)
        print("   %-11s n=%3d avgR=%+.3f ci=+/-%.3f sumR=%+7.2f" % (name, len(xs), m, ci, sum(xs)))
    if cap == 4:
        print("   exit mix (2026):", Counter(t["why"] for t in trades if t["d"] >= "2026-01-01"))
        print("   exit mix (all):", Counter(t["why"] for t in trades))
        per = defaultdict(int)
        for t in trades:
            per[t["sym"]] += 1
        print("   trades per symbol:", dict(sorted(per.items())))
