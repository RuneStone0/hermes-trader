"""Strategy lab — search for a strategy with a REAL edge, on REAL data.

Why this exists
---------------
The nightly `self_improve.py` reviews ~60 closed trades and asks an LLM what it
thinks. That is opinion, not evidence, and at n=60 it cannot settle anything: the
first 61 trades across all sleeves are -133.06 net. This module does the opposite —
it tests candidate rules against 8 years of real daily bars, holds out the final
~2 years, and only calls something "working" if it survives the hold-out.

Hard constraint (measured 2026-11-11): the free IEX data tier serves ~1,584 daily
bars back to 2018-11-01, but only ~1 month of 5-minute bars. So intraday ideas
(ORB) CANNOT be validated here and are not offered as candidates. Daily-bar rules
only. Anything claiming an intraday edge off one month of data would be noise.

Method
------
  IS  : 2018-11-01 .. 2024-12-31   (search space)
  OOS : 2025-01-01 .. today        (hold-out, touched once per config)
A config passes only if OOS is positive AND consistent with IS. Costs are real:
regulatory fees via fees.equity_fees() plus slippage in bp, sized 1% risk/trade
on $25k with a 50% notional cap — the same caps the live sleeves use.

Exploration is cumulative: a ledger of tested configs means each run tests NEW
parameter combinations instead of re-testing the same ones, so a recurring job
keeps widening coverage rather than looping.

Run:  python3 strategy_lab.py --profile weekly --iterations 40
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import random
from datetime import datetime, timedelta, timezone

import config
import fees
from alpaca_rest import AlpacaClient

BARS_DIR = config.DATA_DIR / "bars"
LAB_DIR = config.DATA_DIR / "lab"
LEDGER = LAB_DIR / "tested.jsonl"
JOURNAL = LAB_DIR / "journal.md"
CHAMPION = LAB_DIR / "champion_{profile}.json"

IS_START = "2018-11-01"
IS_END = "2024-12-31"
OOS_A_START, OOS_A_END = "2025-01-01", "2025-12-31"
OOS_B_START = "2026-01-01"

EQUITY = 25_000.0
RISK_FRAC = 0.01          # 1% of equity risked per trade
NOTIONAL_CAP = 0.50       # <= 50% of equity per position
SLIPPAGE_BP = 5.0         # 5 bp each side (liquid ETFs, market orders)

UNIVERSE = ["SPY", "QQQ", "IWM", "GLD", "TLT", "XLE", "XLF", "XLV",
            "XLU", "XLP", "XLB", "XLK", "SMH", "SLV"]

# Per-profile search bias. The profiles differ in horizon, not in ambition.
PROFILES = {
    "daily":  {"families": ["breakout", "rsi2"], "max_hold": (3, 10), "desc": "short horizon"},
    "weekly": {"families": ["pullback", "trend"], "max_hold": (10, 30), "desc": "swing horizon"},
    "yolo":   {"families": ["breakout", "rsi2", "pullback", "trend"], "max_hold": (2, 20), "desc": "wide search"},
}


# --------------------------------------------------------------------------- #
# tiny indicators (inlined: no import-path surprises inside the container)
# --------------------------------------------------------------------------- #
def sma(vals, n):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def atr(bars, n=14):
    trs, out = [], []
    for i, b in enumerate(bars):
        h, l = b["h"], b["l"]
        if i == 0:
            tr = h - l
        else:
            pc = bars[i - 1]["c"]
            tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        if len(trs) >= n:
            out.append(sum(trs[-n:]) / n)
        else:
            out.append(None)
    return out


def rsi(closes, n=2):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def get_bars(client, symbol, refresh_hours=12):
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    path = BARS_DIR / f"{symbol}_1Day.json"
    fresh = (path.exists()
             and (datetime.now(timezone.utc)
                  - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                  ).total_seconds() < refresh_hours * 3600)
    if not fresh:
        d = client.bars(symbol, timeframe="1Day", limit=10000, start=IS_START)
        bars = d.get("bars") or []
        if len(bars) > 100:
            path.write_text(json.dumps(bars))
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    out = []
    for b in raw:
        o = b.get("o", b.get("open"))
        h = b.get("h", b.get("high"))
        l = b.get("l", b.get("low"))
        c = b.get("c", b.get("close"))
        t = b.get("t") or b.get("timestamp")
        if None in (o, h, l, c) or not t:
            continue
        out.append({"t": t[:10], "o": float(o), "h": float(h),
                    "l": float(l), "c": float(c)})
    return out


# --------------------------------------------------------------------------- #
# strategies: each returns a list of (entry_i, exit_i|None, side, stop)
# --------------------------------------------------------------------------- #
def strat_pullback(bars, p):
    closes = [b["c"] for b in bars]
    s = sma(closes, p["sma_n"])
    a = atr(bars, 14)
    sig = []
    for i in range(len(bars)):
        if None in (s[i], a[i]):
            continue
        c = closes[i]
        band = p["band"] * a[i]
        if c > s[i] and c <= s[i] + band:
            sig.append((i, "long"))
        elif p.get("allow_short") and c < s[i] and c >= s[i] - band:
            sig.append((i, "short"))
    return sig


def strat_breakout(bars, p):
    closes = [b["c"] for b in bars]
    n = p["n"]
    sig = []
    for i in range(n + 1, len(bars)):
        hi = max(b["h"] for b in bars[i - n:i])
        lo = min(b["l"] for b in bars[i - n:i])
        if closes[i] > hi:
            sig.append((i, "long"))
        elif p.get("allow_short") and closes[i] < lo:
            sig.append((i, "short"))
    return sig


def strat_rsi2(bars, p):
    closes = [b["c"] for b in bars]
    r = rsi(closes, 2)
    s = sma(closes, p["sma_n"])
    sig = []
    for i in range(len(bars)):
        if r[i] is None or s[i] is None:
            continue
        if r[i] < p["x"] and closes[i] > s[i]:
            sig.append((i, "long"))
    return sig


def strat_trend(bars, p):
    closes = [b["c"] for b in bars]
    f = sma(closes, p["fast"])
    s = sma(closes, p["slow"])
    sig = []
    for i in range(1, len(bars)):
        if None in (f[i], s[i], f[i - 1], s[i - 1]):
            continue
        if f[i - 1] <= s[i - 1] and f[i] > s[i]:
            sig.append((i, "long"))
        elif p.get("allow_short") and f[i - 1] >= s[i - 1] and f[i] < s[i]:
            sig.append((i, "short"))
    return sig


FAMILIES = {"pullback": strat_pullback, "breakout": strat_breakout,
            "rsi2": strat_rsi2, "trend": strat_trend}


# --------------------------------------------------------------------------- #
# simulator
# --------------------------------------------------------------------------- #
def simulate(symbol, bars, family, p, exit_rule):
    sigs = FAMILIES[family](bars, p)
    a = atr(bars, 14)
    trades = []
    busy_until = -1
    for i, side in sigs:
        if i <= busy_until or i + 1 >= len(bars):
            continue
        if a[i] is None or a[i] <= 0:
            continue
        # no lookahead: decide on close i, fill at open i+1
        entry = bars[i + 1]["o"]
        stop_dist = p["stop_atr"] * a[i]
        if stop_dist <= 0:
            continue
        stop = entry - stop_dist if side == "long" else entry + stop_dist
        tgt = None
        if p.get("tgt_atr"):
            tgt = (entry + p["tgt_atr"] * a[i]) if side == "long" else (entry - p["tgt_atr"] * a[i])
        exit_i, exit_px, why = None, None, None
        last = min(len(bars) - 1, i + p["max_hold"])
        for j in range(i + 1, last + 1):
            b = bars[j]
            if side == "long":
                hit_stop = b["l"] <= stop
                hit_tgt = tgt is not None and b["h"] >= tgt
            else:
                hit_stop = b["h"] >= stop
                hit_tgt = tgt is not None and b["l"] <= tgt
            if hit_stop:                      # conservative: stop before target
                exit_i, exit_px, why = j, stop, "stop"
                break
            if hit_tgt:
                exit_i, exit_px, why = j, tgt, "target"
                break
            if exit_rule == "cross" and j == last:
                exit_i, exit_px, why = j, b["c"], "time"
                break
        if exit_i is None:
            exit_i, exit_px, why = last, bars[last]["c"], "time"
        # sizing: 1% risk over the stop distance, capped at 50% notional
        risk_dollars = EQUITY * RISK_FRAC
        shares = risk_dollars / stop_dist
        max_shares = (EQUITY * NOTIONAL_CAP) / entry
        shares = max(1.0, min(shares, max_shares))
        slip = SLIPPAGE_BP / 10_000.0
        fill_in = entry * (1 + slip if side == "long" else 1 - slip)
        fill_out = exit_px * (1 - slip if side == "long" else 1 + slip)
        gross = (fill_out - fill_in) * shares * (1 if side == "long" else -1)
        fee = fees.equity_fees(shares, fill_out, fill_in, sold=True)
        net = gross - fee
        trades.append({"symbol": symbol, "side": side, "entry_i": i, "exit_i": exit_i,
                       "entry": fill_in, "exit": fill_out, "shares": shares,
                       "why": why, "net": net, "r": net / risk_dollars,
                       "days": exit_i - i})
        busy_until = exit_i
    return trades


def metrics(trades):
    if not trades:
        return {"n": 0, "win_rate": 0.0, "expectancy_r": 0.0, "profit_factor": 0.0,
                "sum_r": 0.0, "max_dd_r": 0.0, "net": 0.0, "avg_days": 0.0}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_w, gross_l = sum(wins), abs(sum(losses))
    cum, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": len(trades),
            "win_rate": len(wins) / len(trades),
            "expectancy_r": sum(rs) / len(rs),
            "profit_factor": (gross_w / gross_l) if gross_l > 0 else float("inf"),
            "sum_r": sum(rs), "max_dd_r": dd, "net": sum(t["net"] for t in trades),
            "avg_days": sum(t["days"] for t in trades) / len(trades)}


def score(rec):
    """Rank by the WEAKEST hold-out half, not the pooled total.

    Ranking on pooled OOS rewards whichever config happened to catch one regime
    hardest — the run that motivated this had a champion with B=+3.09 while a
    sibling config scored A=+28.24 / B=+9.89. The sibling is the one you would
    actually trade. min(A, B) is the honest tiebreak.
    """
    return min(rec["holdout_a"]["sum_r"], rec["holdout_b"]["sum_r"])


def _slice(bars, start, end=None):
    return [b for b in bars if b["t"] >= start and (end is None or b["t"] <= end)]


# --------------------------------------------------------------------------- #
# search space
# --------------------------------------------------------------------------- #
def space(family, hold_lo, hold_hi):
    holds = sorted({h for h in (3, 5, 10, 15, 20, 30) if hold_lo <= h <= hold_hi})
    if family == "pullback":
        for sma_n, band, stop_atr, tgt_atr, hold in itertools.product(
                (50, 100, 150), (0.5, 1.0, 1.5), (1.0, 1.5, 2.0), (1.5, 2.5), holds):
            yield {"sma_n": sma_n, "band": band, "stop_atr": stop_atr,
                   "tgt_atr": tgt_atr, "max_hold": hold}
    elif family == "breakout":
        for n, stop_atr, tgt_atr, hold in itertools.product(
                (20, 55, 100), (1.5, 2.0, 3.0), (0, 2.0, 4.0), holds):
            yield {"n": n, "stop_atr": stop_atr, "tgt_atr": tgt_atr, "max_hold": hold}
    elif family == "rsi2":
        for x, y, sma_n, stop_atr, hold in itertools.product(
                (5, 10, 15), (200,), (100, 200), (1.5, 2.0), holds):
            yield {"x": x, "y": y, "sma_n": sma_n, "stop_atr": stop_atr,
                   "tgt_atr": 0, "max_hold": hold}
    elif family == "trend":
        for fast, slow, stop_atr, hold in itertools.product(
                (10, 20, 50), (100, 200), (2.0, 3.0), holds):
            if fast >= slow:
                continue
            yield {"fast": fast, "slow": slow, "stop_atr": stop_atr,
                   "tgt_atr": 0, "max_hold": hold}


def key_of(profile, family, p):
    raw = json.dumps([profile, family, p], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def tested_keys():
    if not LEDGER.exists():
        return set()
    out = set()
    for line in LEDGER.read_text().splitlines():
        try:
            out.add(json.loads(line)["key"])
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- #
# one evaluation
# --------------------------------------------------------------------------- #
def evaluate(family, p, bars_by_sym, exit_rule="bracket"):
    """Three disjoint windows.

    The hold-out is split in two on purpose. Searching 264 configs and then
    picking the best single OOS number is selection bias: at a 5% false-positive
    rate you would expect ~13 spurious "winners" from noise alone. Requiring the
    SAME config to be positive in BOTH halves of the hold-out is a much weaker
    claim than one pooled number, and it rejects edges that only exist in one
    regime.
    """
    is_tr, oa_tr, ob_tr = [], [], []
    for sym, bars in bars_by_sym.items():
        if len(bars) < 260:
            continue
        is_bars = _slice(bars, IS_START, IS_END)
        oos_a = _slice(bars, OOS_A_START, OOS_A_END)
        oos_b = _slice(bars, OOS_B_START)
        if len(is_bars) > 120:
            is_tr += simulate(sym, is_bars, family, p, exit_rule)
        if len(oos_a) > 40:
            oa_tr += simulate(sym, oos_a, family, p, exit_rule)
        if len(oos_b) > 40:
            ob_tr += simulate(sym, oos_b, family, p, exit_rule)
    oos_tr = oa_tr + ob_tr
    return metrics(is_tr), metrics(oa_tr), metrics(ob_tr), metrics(oos_tr), oos_tr


def passes(im, ma, mb, om):
    reasons = []
    if om["n"] < 20:
        reasons.append(f"oos n={om['n']} < 20")
    if om["sum_r"] <= 0:
        reasons.append(f"oos sumR={om['sum_r']:.2f} <= 0")
    if om["profit_factor"] < 1.15:
        reasons.append(f"oos PF={om['profit_factor']:.2f} < 1.15")
    if im["sum_r"] <= 0:
        reasons.append(f"is sumR={im['sum_r']:.2f} <= 0 (inconsistent)")
    if ma["n"] < 5 or ma["sum_r"] <= 0:
        reasons.append(f"holdout-A n={ma['n']} sumR={ma['sum_r']:.2f} (must both be +)")
    if mb["n"] < 5 or mb["sum_r"] <= 0:
        reasons.append(f"holdout-B n={mb['n']} sumR={mb['sum_r']:.2f} (must both be +)")
    if om["max_dd_r"] > 20:
        reasons.append(f"oos dd={om['max_dd_r']:.1f}R > 20R")
    return (not reasons), reasons


def rank_from_ledger(profile):
    """Re-rank everything already tested for this profile, no re-run needed."""
    if not LEDGER.exists():
        return None, []
    recs = []
    for line in LEDGER.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("profile") != profile or not r.get("passed"):
            continue
        if "holdout_a" not in r or "holdout_b" not in r:
            continue          # tested under the older single-OOS gate
        recs.append(r)
    if not recs:
        return None, []
    recs.sort(key=score, reverse=True)
    return recs[0], recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=sorted(PROFILES))
    ap.add_argument("--iterations", type=int, default=40)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--rank-only", action="store_true",
                    help="re-rank the existing ledger, test nothing new")
    args = ap.parse_args()

    if args.rank_only:
        best, recs = rank_from_ledger(args.profile)
        if not best:
            print(f"[lab] no passing configs in the ledger for {args.profile}")
            return
        (LAB_DIR / f"champion_{args.profile}.json").write_text(json.dumps(best, indent=2))
        print(f"[lab] {args.profile}: {len(recs)} passing configs in the ledger, "
              f"ranked by weakest hold-out half")
        for r in recs[:6]:
            print(f"  min(A,B)={score(r):>7.2f}  oos={r['oos']['sum_r']:>7.2f} "
                  f"is={r['is']['sum_r']:>7.2f}  PF={r['oos']['profit_factor']:.2f} "
                  f"n={r['oos']['n']:>3}  {r['family']} {r['params']}")
        print(f"[lab] CHAMPION: {best['family']} {best['params']}")
        return

    prof = PROFILES[args.profile]
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed if args.seed is not None else
                        int(datetime.now(timezone.utc).timestamp()))
    client = AlpacaClient("daily")

    print(f"[lab] profile={args.profile} ({prof['desc']}) iterations={args.iterations}")
    bars_by_sym = {}
    for sym in UNIVERSE:
        try:
            b = get_bars(client, sym)
            if len(b) > 260:
                bars_by_sym[sym] = b
        except Exception as e:
            print(f"[lab] {sym}: fetch failed {type(e).__name__}: {e}")
    print(f"[lab] universe: {len(bars_by_sym)} symbols, "
          f"{min((len(b) for b in bars_by_sym.values()), default=0)}-"
          f"{max((len(b) for b in bars_by_sym.values()), default=0)} bars each")

    done = tested_keys()
    candidates = []
    for fam in prof["families"]:
        for p in space(fam, *prof["max_hold"]):
            k = key_of(args.profile, fam, p)
            if k not in done:
                candidates.append((fam, p, k))
    rng.shuffle(candidates)
    print(f"[lab] {len(candidates)} untested configs available, "
          f"evaluating {min(args.iterations, len(candidates))}")

    results, best = [], None
    for fam, p, k in candidates[:args.iterations]:
        im, ma, mb, om, _ = evaluate(fam, p, bars_by_sym)
        ok, why = passes(im, ma, mb, om)
        rec = {"key": k, "profile": args.profile, "family": fam, "params": p,
               "is": im, "holdout_a": ma, "holdout_b": mb, "oos": om,
               "passed": ok, "rejected": why,
               "ts": datetime.now(timezone.utc).isoformat()}
        results.append(rec)
        with LEDGER.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        flag = "PASS" if ok else "fail"
        print(f"  {flag} {fam:9} oos n={om['n']:>3} sumR={om['sum_r']:>7.2f} "
              f"PF={om['profit_factor']:>5.2f} is={im['sum_r']:>7.2f} "
              f"A={ma['sum_r']:>5.2f} B={mb['sum_r']:>5.2f}  {p}")
        if ok and (best is None or score(rec) > score(best)):
            best = rec

    # journal
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with JOURNAL.open("a") as f:
        f.write(f"\n## {args.profile} · {now} · {len(results)} configs\n\n")
        passed = [r for r in results if r["passed"]]
        f.write(f"- tested {len(results)}, passed {len(passed)}\n")
        if best:
            f.write(f"- CHAMPION: {best['family']} {best['params']} "
                    f"oos sumR={best['oos']['sum_r']:.2f} PF={best['oos']['profit_factor']:.2f} "
                    f"(A={best['holdout_a']['sum_r']:.2f} B={best['holdout_b']['sum_r']:.2f})\n")
        else:
            f.write("- no config passed the hold-out gate this run\n")
            top = sorted(results, key=lambda r: r["oos"]["sum_r"], reverse=True)[:3]
            for t in top:
                f.write(f"  - closest: {t['family']} {t['params']} "
                        f"oos sumR={t['oos']['sum_r']:.2f} PF={t['oos']['profit_factor']:.2f} "
                        f"(A={t['holdout_a']['sum_r']:.2f} B={t['holdout_b']['sum_r']:.2f}; "
                        f"{'; '.join(t['rejected'])})\n")

    if best:
        champ_path = LAB_DIR / f"champion_{args.profile}.json"
        champ_path.write_text(json.dumps(best, indent=2))
        print(f"\n[lab] CHAMPION written for {args.profile}: {best['family']} {best['params']}")
        print(f"[lab] oos sumR={best['oos']['sum_r']:.2f} "
              f"PF={best['oos']['profit_factor']:.2f} n={best['oos']['n']} "
              f"(A={best['holdout_a']['sum_r']:.2f} B={best['holdout_b']['sum_r']:.2f})")
    else:
        print(f"\n[lab] no config passed for {args.profile} this run")
    print(f"[lab] journal: {JOURNAL}")


if __name__ == "__main__":
    main()
