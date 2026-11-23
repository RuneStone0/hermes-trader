"""Independent replay of the lab's 20-day-high breakout family (daily bars).

Question: is the plateau real on my own code, and does it survive in the CURRENT
tape (Sep-Nov 2026), not just the 2026 average? Read-only.
"""
import math

from alpaca_rest import AlpacaClient
from indicators import atr

UNIVERSE = ["SPY", "QQQ", "IWM", "GLD", "TLT", "XLE", "XLF", "XLV",
            "XLU", "XLP", "XLB", "XLK", "SMH", "SLV"]
START = "2020-07-27"
SLIP = 0.0001

CONFIGS = [
    {"n": 20, "stop_atr": 1.5, "tgt_atr": 4.0, "max_hold": 15},
    {"n": 20, "stop_atr": 2.0, "tgt_atr": 4.0, "max_hold": 10},
    {"n": 20, "stop_atr": 3.0, "tgt_atr": 4.0, "max_hold": 20},
    {"n": 20, "stop_atr": 1.5, "tgt_atr": 0.0, "max_hold": 10},
    {"n": 55, "stop_atr": 1.5, "tgt_atr": 4.0, "max_hold": 15},
]

client = AlpacaClient("weekly")
data = {}
for sym in UNIVERSE:
    resp = client.bars(sym, "1Day", limit=2000, start=START)
    data[sym] = [{"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "t": b["t"][:10]}
                 for b in (resp.get("bars") or [])]
print("data:", {k: len(v) for k, v in data.items()})


def mean_ci(xs):
    n = len(xs)
    if n == 0:
        return (0.0, 0.0)
    m = sum(xs) / n
    if n < 2:
        return (m, 0.0)
    sd = (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
    return (m, 1.96 * sd / math.sqrt(n))


def sim(sym, bars, p):
    a = atr(bars, 14)
    out = []
    busy = -1
    for i in range(len(bars) - 1):
        if i <= busy or i < p["n"] + 20 or a[i] is None or a[i] <= 0:
            continue
        hi = max(b["h"] for b in bars[i - p["n"]:i])
        if bars[i]["c"] <= hi:
            continue
        entry = bars[i + 1]["o"] * (1 + SLIP)
        stop = entry - p["stop_atr"] * a[i]
        tgt = (entry + p["tgt_atr"] * a[i]) if p["tgt_atr"] else None
        if stop <= 0 or entry <= stop:
            continue
        last = min(len(bars) - 1, i + 1 + p["max_hold"])
        exit_px, why, j = None, None, last
        for k in range(i + 1, last + 1):
            if bars[k]["l"] <= stop:
                exit_px, why, j = stop, "stop", k
                break
            if tgt is not None and bars[k]["h"] >= tgt:
                exit_px, why, j = tgt, "target", k
                break
        if exit_px is None:
            exit_px, why = bars[last]["c"], "time"
        r = (exit_px * (1 - SLIP) - entry) / (entry - stop)
        out.append({"sym": sym, "d": bars[i + 1]["t"], "R": r, "why": why})
        busy = j
    return out


for p in CONFIGS:
    trades = []
    for sym in UNIVERSE:
        trades += sim(sym, data[sym], p)
    trades.sort(key=lambda t: t["d"])
    def win(lo, hi):
        return [t["R"] for t in trades if lo <= t["d"] <= hi]
    label = "n=%d stop=%.1f tgt=%.1f hold=%d" % (p["n"], p["stop_atr"], p["tgt_atr"], p["max_hold"])
    parts = []
    for name, lo, hi in (("2020H2-22", "2020-07-27", "2022-12-31"), ("2023-24", "2023-01-01", "2024-12-31"),
                         ("2025", "2025-01-01", "2025-12-31"), ("2026", "2026-01-01", "2026-12-31"),
                         ("2026H2", "2026-07-01", "2026-12-31"), ("since Sep", "2026-09-01", "2026-12-31")):
        xs = win(lo, hi)
        m, ci = mean_ci(xs)
        parts.append("%s n=%d avgR=%+.3f sumR=%+.1f" % (name, len(xs), m, sum(xs)))
    print("\n" + label)
    for x in parts:
        print("   ", x)
