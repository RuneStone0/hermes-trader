#!/usr/bin/env python3
"""Read-only: replay an exit-rule change against the REAL closed book.

Question it answers: "would banking part of the position at +T·R have made this book
better?" — the class of proposal that keeps arriving ("scale out at the measured median
excursion", "take profit earlier", "trail harder").

Why a bar-path replay and not the stored excursions: `mfe_r` says a level was TOUCHED
somewhere inside the holding window, not that it was touched BEFORE the stop. A trade
that hit -1R and later printed a +0.6R high (or whose stored MFE came from a truncated
window / a mis-signed side) looks bankable in the columns and was not bankable in the
tape. So this walks the 5-min bars in order and requires the level to trade before the
stop; a bar containing BOTH is resolved as "not bankable" (conservative).

Model of the change: sell `--frac` of the position at the level, leave the rest with its
existing stop/target and the exit it actually got, plus one extra sell fee.

    docker exec -w /app -e PYTHONPATH=/app hermes-trader \
        python3 /app/probes/replay_exit_rule.py --strategy yolo --levels 0.25,0.35,0.5,1.0

Every number is computed from the trade rows + 5-min bars. No writes, no orders.
"""
import argparse
import sys
from datetime import timedelta

import db
import r_telemetry
from alpaca_rest import AlpacaClient, AlpacaError


def risk_of(t) -> float | None:
    if not t["stop_price"]:
        return None
    return abs(float(t["entry_price"]) - float(t["stop_price"])) * float(t["qty"])


def r_of(t) -> float | None:
    k = risk_of(t)
    if not k or t["net_pnl"] is None:
        return None
    return float(t["net_pnl"]) / k


def touch_before_stop(win, t, level: float) -> tuple[bool, bool]:
    """(traded, ambiguous) — did `level` trade before the stop, in bar order?"""
    side = t["side"]
    entry, stop = float(t["entry_price"]), float(t["stop_price"])
    risk = abs(entry - stop)
    lvl = entry + level * risk if side == "long" else entry - level * risk
    for _ts, hi, lo in win:
        fav = hi >= lvl if side == "long" else lo <= lvl
        adv = lo <= stop if side == "long" else hi >= stop
        if fav and adv:
            return False, True          # same bar: assume the stop won
        if adv:
            return False, False
        if fav:
            return True, False
    return False, False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="yolo")
    ap.add_argument("--levels", default="0.25,0.30,0.35,0.40,0.50,0.75,1.00")
    ap.add_argument("--frac", type=float, default=0.5, help="fraction banked at the level")
    ap.add_argument("--account", default=None)
    args = ap.parse_args()

    levels = [float(x) for x in args.levels.split(",")]
    rows = [t for t in db.all_trades(args.account)
            if t["status"] == "closed" and t["strategy"] == args.strategy
            and r_of(t) is not None]
    if not rows:
        print(f"no closed {args.strategy} trades with a computable R")
        return 1
    client = AlpacaClient(rows[0]["account"])
    rows.sort(key=lambda t: r_telemetry.entry_ts(t) or t["created_at"])
    rv = {t["id"]: float(r_of(t)) for t in rows}

    base = sum(rv.values())
    print(f"book={args.strategy} closed={len(rows)} sumR={base:+.2f} "
          f"avgR={base/len(rows):+.3f} bank_frac={args.frac:.0%}")
    print("\n  id sym   side    R    bars  " + "  ".join(f"{lv:.2f}" for lv in levels))
    paths: dict[int, dict[float, bool]] = {}
    for t in rows:
        t0 = r_telemetry.entry_ts(t)
        t1 = r_telemetry._ts(t["exit_time"]) or r_telemetry._ts(t["updated_at"])
        win = r_telemetry._bars_in_window(client, t, t0, t1) if (t0 and t1 and t1 > t0) else []
        paths[t["id"]] = {lv: touch_before_stop(win, t, lv)[0] for lv in levels}
        flag = "".join("Y" if paths[t["id"]][lv] else "." for lv in levels)
        print(f"{t['id']:>4} {t['symbol']:<5}{t['side']:<7}{rv[t['id']]:+6.2f}{len(win):>6}  {flag}")

    print("\nlevel  n_bank  sumR_new   delta   avg_delta")
    for lv in levels:
        new_sum, deltas = 0.0, []
        for t in rows:
            R = rv[t["id"]]
            if paths[t["id"]][lv]:
                extra_fee = 0.01 + 0.0000278 * args.frac * float(t["qty"]) * float(t["entry_price"])
                newR = R + args.frac * (lv - R) - extra_fee / float(risk_of(t) or 1.0)
            else:
                newR = R
            new_sum += newR
            deltas.append(newR - R)
        nb = sum(1 for t in rows if paths[t["id"]][lv])
        print(f"{lv:<6.2f} {nb:>6} {new_sum:>+9.2f} {new_sum-base:>+7.2f} {sum(deltas)/len(deltas):>+11.3f}")
    print("\nA change ships only if the delta is POSITIVE and its out-of-sample half holds:"
          " re-run with --levels for the chosen level and compare the first/second half by hand.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AlpacaError as exc:
        print(f"data error: {exc}", file=sys.stderr)
        raise SystemExit(2)
