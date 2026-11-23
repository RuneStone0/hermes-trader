"""Weekly review 2026-11-23: does the portfolio sleeve's pullback rule work when
SPY is BELOW its 200d? Replays the live _signal rule on real daily bars and tags
every entry by the regime that was knowable at the signal close.

Read-only: no orders, no writes.
"""
import math
from collections import defaultdict

import config
from alpaca_rest import AlpacaClient
from indicators import sma, atr

UNIVERSE = ["SPY", "QQQ", "IWM", "GLD", "TLT", "XLE", "XLF", "XLV",
            "XLU", "XLP", "XLB", "XLK", "SMH", "SLV"]
START = "2020-07-27"
SLIP = 0.0001          # 1 bp adverse per side (backtest_mr.SLIP_BPS)
STOP_ATR, TGT_ATR = 1.5, 2.5
MAX_HOLD = 30          # trading sessions (~42 calendar days)

client = AlpacaClient("weekly")

data = {}
for sym in UNIVERSE:
    resp = client.bars(sym, "1Day", limit=2000, start=START)
    bars = resp.get("bars") or []
    if not bars:
        print("NO BARS", sym)
        continue
    data[sym] = [{"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"], "t": b["t"][:10]} for b in bars]
    print("bars %-5s %d  %s .. %s" % (sym, len(bars), bars[0]["t"][:10], bars[-1]["t"][:10]))

spy = data["SPY"]
spy_close = [b["c"] for b in spy]
spy_sma200 = sma(spy_close, 200)
spy_sma20 = sma(spy_close, 20)
spy_by_date = {b["t"]: i for i, b in enumerate(spy)}

regime = {}
below_days = 0
for i, b in enumerate(spy):
    if spy_sma200[i] is None:
        continue
    ab200 = b["c"] > spy_sma200[i]
    ab20 = (spy_sma20[i] is not None) and (b["c"] > spy_sma20[i])
    regime[b["t"]] = (ab200, ab20)
    if not ab200:
        below_days += 1
print("\nSPY trading days with a 200d SMA: %d, of which below: %d (%.1f%%)"
      % (len(regime), below_days, 100.0 * below_days / max(1, len(regime))))


def mean_ci(xs):
    n = len(xs)
    if n == 0:
        return (0.0, 0.0, 0.0, 0.0)
    m = sum(xs) / n
    if n < 2:
        return (m, 0.0, 0.0, 0.0)
    sd = (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
    se = sd / math.sqrt(n)
    return (m, se, m - 1.96 * se, m + 1.96 * se)


def bucket_of(date):
    if date <= "2024-12-31":
        return "IS 2020-07..2024"
    if date <= "2025-12-31":
        return "OOS-A 2025"
    return "OOS-B 2026"


trades = []
for sym in UNIVERSE:
    bars = data[sym]
    closes = [b["c"] for b in bars]
    s50 = sma(closes, 50)
    a14 = atr(bars, 14)
    i = 0
    n = len(bars)
    while i < n - 1:
        if (i < 52 or s50[i] is None or a14[i] is None or a14[i] <= 0):
            i += 1
            continue
        c, sm, av = closes[i], s50[i], a14[i]
        inside = (c > sm) and (c <= sm + 1.0 * av)
        if not inside:
            i += 1
            continue
        entry = bars[i + 1]["o"] * (1 + SLIP)
        stop = c - STOP_ATR * av
        tgt = c + TGT_ATR * av
        if stop <= 0 or entry <= stop:
            i += 1
            continue
        r_den = entry - stop
        hit = None
        for j in range(i + 1, min(i + 1 + MAX_HOLD, n)):
            lo, hi = bars[j]["l"], bars[j]["h"]
            if lo <= stop:
                hit = ("stop", stop)
                break
            if hi >= tgt:
                hit = ("target", tgt)
                break
        if hit is None:
            j = min(i + MAX_HOLD, n - 1)
            hit = ("time", bars[j]["c"])
        reason, px = hit
        exit_px = px * (1 - SLIP)
        r = (exit_px - entry) / r_den
        dt = bars[i + 1]["t"]
        reg = regime.get(bars[i]["t"]) or regime.get(dt)
        trades.append({"sym": sym, "date": dt, "signal_date": bars[i]["t"], "R": r,
                       "ab200": None if reg is None else reg[0],
                       "ab20": None if reg is None else reg[1],
                       "reason": reason, "bucket": bucket_of(dt)})
        # one position per symbol: resume after the exit bar
        i = j + 1

print("\nTOTAL simulated trades: %d" % len(trades))
for bk in ("IS 2020-07..2024", "OOS-A 2025", "OOS-B 2026"):
    sel = [t for t in trades if t["bucket"] == bk]
    m, se, lo, hi = mean_ci([t["R"] for t in sel])
    print("  %-18s n=%4d sumR=%+8.2f avgR=%+.3f ci95=[%+.3f,%+.3f] win%%=%.1f"
          % (bk, len(sel), sum(t["R"] for t in sel), m, lo, hi,
             100.0 * sum(1 for t in sel if t["R"] > 0) / max(1, len(sel))))

print("\n== REGIME BUCKETS (SPY vs 200d SMA at signal close) ==")
for label, key in (("SPY ABOVE 200d", True), ("SPY BELOW 200d", False)):
    sel = [t for t in trades if t["ab200"] is key]
    m, se, lo, hi = mean_ci([t["R"] for t in sel])
    print("  %-16s n=%4d sumR=%+8.2f avgR=%+.3f ci95=[%+.3f,%+.3f] win%%=%.1f"
          % (label, len(sel), sum(t["R"] for t in sel), m, lo, hi,
             100.0 * sum(1 for t in sel if t["R"] > 0) / max(1, len(sel))))
    for bk in ("IS 2020-07..2024", "OOS-A 2025", "OOS-B 2026"):
        s2 = [t for t in sel if t["bucket"] == bk]
        if s2:
            m2, _, lo2, hi2 = mean_ci([t["R"] for t in s2])
            print("      %-18s n=%4d sumR=%+7.2f avgR=%+.3f ci95=[%+.3f,%+.3f]"
                  % (bk, len(s2), sum(t["R"] for t in s2), m2, lo2, hi2))

A = [t["R"] for t in trades if t["ab200"] is True]
B = [t["R"] for t in trades if t["ab200"] is False]
if len(A) > 1 and len(B) > 1:
    ma, mb = sum(A) / len(A), sum(B) / len(B)
    va = sum((x - ma) ** 2 for x in A) / (len(A) - 1)
    vb = sum((x - mb) ** 2 for x in B) / (len(B) - 1)
    se_d = (va / len(A) + vb / len(B)) ** 0.5
    tstat = (ma - mb) / se_d if se_d else 0.0
    print("\nDIFFERENCE (above - below): %+.4f R, se=%.4f, Welch t=%+.2f, 95%% CI [%+.3f, %+.3f]"
          % (ma - mb, se_d, tstat, ma - mb - 1.96 * se_d, ma - mb + 1.96 * se_d))

# What the gate would keep: only entries taken while SPY is above 200d
keep = [t for t in trades if t["ab200"] is True]
print("\nGATE VARIANT (only SPY>200d): n=%d (%.0f%% of trades kept) sumR=%+.2f avgR=%+.3f"
      % (len(keep), 100.0 * len(keep) / max(1, len(trades)), sum(t["R"] for t in keep), mean_ci([t["R"] for t in keep])[0]))
for bk in ("IS 2020-07..2024", "OOS-A 2025", "OOS-B 2026"):
    s2 = [t for t in keep if t["bucket"] == bk]
    print("   %-18s n=%4d sumR=%+7.2f" % (bk, len(s2), sum(t["R"] for t in s2)))

print("\n== 2026 trades (OOS-B) detail ==")
for t in trades:
    if t["bucket"] == "OOS-B 2026":
        print("  %s %-5s R=%+.2f ab200=%s ab20=%s %s" % (t["date"], t["sym"], t["R"], t["ab200"], t["ab20"], t["reason"]))
