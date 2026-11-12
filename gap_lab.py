"""Gap-fill strategy lab — does "fade the opening gap, exit when it fills" work?

The rule (as specified): at the open, if price gapped DOWN from the previous
close, buy and exit when the gap closes; if it gapped UP, sell and exit when the
gap closes. Intraday, flat by the close, modest stop.

Why it is measurable on DAILY bars (and why that is better here)
---------------------------------------------------------------
A gap needs exactly two prices: the previous close and today's open. Both are in
the daily bar. "Did the gap fill intraday" is also in the daily bar: a gap-down
filled if today's HIGH reached the previous close; a gap-up filled if today's LOW
did. So the whole strategy is expressible without a single intraday bar — which
matters a lot here, because the free IEX tier serves only ~800 intraday bars per
window (~5 weeks), while daily bars go back to 2018-11. One month cannot settle
whether a 0.2% gap fills; eight years can.

The one thing daily bars cannot resolve is the ORDER of events inside a day. If
a day touches both the stop and the gap target, we assume the STOP was hit first.
That biases every result DOWNWARD, which is the safe direction for deciding
whether to trade something.

Instruments: SPX and NDX are indices and are not tradeable (and Alpaca returns
nothing for them). SPY (SPX/10) and QQQ (NDX proxy) carry the same gap structure
and are what this actually trades.

Run: python3 gap_lab.py [--symbols SPY,QQQ] [--iterations 60]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import config
import fees
from alpaca_rest import AlpacaClient
import strategy_lab as L

EQUITY = 25_000.0
RISK_FRAC = 0.01
NOTIONAL_CAP = 0.50
SLIPPAGE_BP = 5.0

JOURNAL = config.DATA_DIR / "lab" / "gap_journal.md"
LEDGER = config.DATA_DIR / "lab" / "gap_tested.jsonl"


def simulate_gap(symbol, bars, p):
    """Enter at the OPEN on a qualifying gap; exit on fill, stop, or the close."""
    trades = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1]["c"]
        b = bars[i]
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        if prev_close <= 0 or o <= 0:
            continue
        gap_pct = (o - prev_close) / prev_close
        ag = abs(gap_pct)
        if ag < p["min_gap"] or ag > p["max_gap"]:
            continue

        # "fill" fades the gap (the requested strategy); "continue" trades WITH it,
        # which is the diagnostic for whether the edge is simply inverted.
        if p.get("direction", "fill") == "fill":
            long = gap_pct < 0                  # gapped down -> buy the fill
            target = prev_close
        else:
            long = gap_pct > 0                  # gapped up -> buy the continuation
            target = None                       # no gap-close target; stop or EOD
        if p["stop_mode"] == "none":
            stop = None
        elif p["stop_mode"] == "pct":
            stop = o * (1 - p["stop"] / 100.0) if long else o * (1 + p["stop"] / 100.0)
        else:                                    # "gap": stop beyond the gap, in gap units
            stop = o - p["stop"] * ag * o if long else o + p["stop"] * ag * o

        risk_per_share = abs(o - stop) if stop is not None else (ag * o)
        if risk_per_share <= 0:
            continue
        risk_dollars = EQUITY * RISK_FRAC
        shares = risk_dollars / risk_per_share
        shares = max(1.0, min(shares, (EQUITY * NOTIONAL_CAP) / o))

        # conservative intrabar ordering: stop before target
        if stop is None:
            hit_stop = False
        elif long:
            hit_stop = l <= stop
        else:
            hit_stop = h >= stop
        if long:
            hit_tgt = target is not None and h >= target
        else:
            hit_tgt = target is not None and l <= target

        if hit_stop:
            exit_px, why = stop, "stop"
        elif hit_tgt:
            exit_px, why = target, "fill"
        else:
            exit_px, why = c, "eod"

        slip = SLIPPAGE_BP / 10_000.0
        fill_in = o * (1 + slip if long else 1 - slip)
        fill_out = exit_px * (1 - slip if long else 1 + slip)
        gross = (fill_out - fill_in) * shares * (1 if long else -1)
        fee = fees.equity_fees(shares, fill_out, fill_in, sold=True)
        net = gross - fee
        trades.append({"symbol": symbol, "long": long, "date": b["t"], "gap_pct": gap_pct,
                       "why": why, "net": net, "r": net / risk_dollars})
    return trades


def metrics(trades):
    return L.metrics([{"r": t["r"], "net": t["net"], "days": 0} for t in trades])


def evaluate(symbols, bars_by_sym, p):
    is_tr, oa_tr, ob_tr = [], [], []
    for sym in symbols:
        bars = bars_by_sym.get(sym) or []
        if len(bars) < 200:
            continue
        is_tr += simulate_gap(sym, L._slice(bars, L.IS_START, L.IS_END), p)
        oa_tr += simulate_gap(sym, L._slice(bars, L.OOS_A_START, L.OOS_A_END), p)
        ob_tr += simulate_gap(sym, L._slice(bars, L.OOS_B_START), p)
    oos_tr = oa_tr + ob_tr
    return metrics(is_tr), metrics(oa_tr), metrics(ob_tr), metrics(oos_tr), oos_tr


def passes(im, ma, mb, om):
    reasons = []
    if om["n"] < 30:
        reasons.append(f"oos n={om['n']} < 30")
    if om["sum_r"] <= 0:
        reasons.append(f"oos sumR={om['sum_r']:.2f} <= 0")
    if om["profit_factor"] < 1.15:
        reasons.append(f"oos PF={om['profit_factor']:.2f} < 1.15")
    if im["sum_r"] <= 0:
        reasons.append(f"is sumR={im['sum_r']:.2f} <= 0")
    if ma["n"] < 10 or ma["sum_r"] <= 0:
        reasons.append(f"2025 n={ma['n']} sumR={ma['sum_r']:.2f}")
    if mb["n"] < 10 or mb["sum_r"] <= 0:
        reasons.append(f"2026 n={mb['n']} sumR={mb['sum_r']:.2f}")
    if om["max_dd_r"] > 15:
        reasons.append(f"oos dd={om['max_dd_r']:.1f}R > 15R")
    return (not reasons), reasons


def space():
    for min_gap in (0.10, 0.20, 0.30, 0.50):
        for max_gap in (1.0, 2.0, 3.0):
            for mode, stops in (("pct", (0.25, 0.50, 0.75)), ("gap", (0.5, 1.0, 1.5))):
                for stop in stops:
                    if max_gap <= min_gap:
                        continue
                    yield {"min_gap": min_gap / 100.0, "max_gap": max_gap / 100.0,
                           "stop_mode": mode, "stop": stop}
    # diagnostic: no stop at all, hold to fill-or-close. Isolates whether any
    # edge exists in the mechanism itself, independent of stop placement.
    for min_gap in (0.10, 0.20, 0.30, 0.50):
        for max_gap in (1.0, 2.0, 3.0):
            if max_gap > min_gap:
                yield {"min_gap": min_gap / 100.0, "max_gap": max_gap / 100.0,
                       "stop_mode": "none", "stop": 0}
    # diagnostic: trade WITH the gap instead of against it
    for min_gap in (0.10, 0.20, 0.30, 0.50):
        for max_gap in (1.0, 2.0, 3.0):
            for mode, stops in (("pct", (0.25, 0.50)), ("none", (0,))):
                for stop in stops:
                    if max_gap > min_gap:
                        yield {"min_gap": min_gap / 100.0, "max_gap": max_gap / 100.0,
                               "stop_mode": mode, "stop": stop,
                               "direction": "continue"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ")
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--slippage-bp", type=float, default=5.0,
                    help="per-side slippage in bp; SPY/QQQ spreads are ~1bp")
    args = ap.parse_args()
    global SLIPPAGE_BP
    SLIPPAGE_BP = args.slippage_bp
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    client = AlpacaClient("daily")
    bars_by_sym = {}
    for s in syms:
        b = L.get_bars(client, s)
        if len(b) > 200:
            bars_by_sym[s] = b
            print(f"[gap] {s}: {len(b)} daily bars "
                  f"{b[0]['t']} .. {b[-1]['t']}")
    if not bars_by_sym:
        raise SystemExit("no bars")

    done = set()
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                done.add(json.loads(line)["key"])
            except Exception:
                continue

    results, best = [], None
    for p in space():
        key = json.dumps([syms, p], sort_keys=True)
        if key in done:
            continue
        im, ma, mb, om, _ = evaluate(syms, bars_by_sym, p)
        ok, why = passes(im, ma, mb, om)
        rec = {"key": key, "symbols": syms, "params": p, "is": im,
               "holdout_a": ma, "holdout_b": mb, "oos": om, "passed": ok,
               "rejected": why, "ts": datetime.now(timezone.utc).isoformat()}
        results.append(rec)
        with LEDGER.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        flag = "PASS" if ok else "fail"
        print(f"  {flag} gap>{p['min_gap']*100:.1f}% <{p['max_gap']*100:.0f}% "
              f"stop={p['stop_mode']}:{p['stop']} | is={im['sum_r']:>7.2f} "
              f"A={ma['sum_r']:>6.2f} B={mb['sum_r']:>6.2f} oos n={om['n']:>3} "
              f"sumR={om['sum_r']:>7.2f} PF={om['profit_factor']:>5.2f} "
              f"win={om['win_rate']:.0%}")
        if ok and (best is None or min(ma["sum_r"], mb["sum_r"]) > min(best["holdout_a"]["sum_r"], best["holdout_b"]["sum_r"])):
            best = rec

    # fill-rate diagnostics: the core mechanism, independent of sizing
    print("\n[gap] fill-rate by gap bucket (all qualifying gaps, no stop):")
    for sym, bars in bars_by_sym.items():
        buckets = {}
        for i in range(1, len(bars)):
            pc, b = bars[i - 1]["c"], bars[i]
            if pc <= 0:
                continue
            g = (b["o"] - pc) / pc
            ag = abs(g)
            if ag < 0.001:
                continue
            k = f"{ag*100:.1f}%" if ag < 0.01 else ("1-2%" if ag < 0.02 else ">2%")
            filled = (b["h"] >= pc) if g < 0 else (b["l"] <= pc)
            d = buckets.setdefault(k, [0, 0])
            d[0] += 1
            d[1] += 1 if filled else 0
        for k in sorted(buckets):
            n, f = buckets[k]
            if n >= 10:
                print(f"    {sym:4} gap {k:>5}: filled {f}/{n} = {f/n:.0%}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with JOURNAL.open("a") as f:
        f.write(f"\n## gap-fill · {','.join(syms)} · {now} · {len(results)} configs\n\n")
        if best:
            f.write(f"- CHAMPION: {best['params']} oos sumR={best['oos']['sum_r']:.2f} "
                    f"PF={best['oos']['profit_factor']:.2f} n={best['oos']['n']} "
                    f"(A={best['holdout_a']['sum_r']:.2f} B={best['holdout_b']['sum_r']:.2f})\n")
        else:
            f.write("- nothing passed the hold-out gate\n")
            for t in sorted(results, key=lambda r: r["oos"]["sum_r"], reverse=True)[:3]:
                f.write(f"  - closest: {t['params']} oos sumR={t['oos']['sum_r']:.2f} "
                        f"({'; '.join(t['rejected'])})\n")
    if best:
        (JOURNAL.parent / "champion_gap.json").write_text(json.dumps(best, indent=2))
        print(f"\n[gap] CHAMPION: {best['params']}")
        print(f"[gap] oos sumR={best['oos']['sum_r']:.2f} "
              f"PF={best['oos']['profit_factor']:.2f} n={best['oos']['n']} "
              f"win={best['oos']['win_rate']:.0%}")
    else:
        print("\n[gap] nothing passed")
    print(f"[gap] journal: {JOURNAL}")


if __name__ == "__main__":
    main()
