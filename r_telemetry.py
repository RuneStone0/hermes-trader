"""R-multiple telemetry: MFE/MAE and stop distance in ATR for closed trades.

WHY THIS EXISTS
---------------
For four weeks every review cycle wrote the same sentence in a different wording:
"with n trades and stop_out_share ~0.2 we cannot tell whether the EXIT rule is
cutting winners short or the ENTRIES simply never follow through" (DAILY 3/3
winners with best_R 0.22 against an rr_multiple of 1.5; YOLO 1/13 with
stop_out_share 0.23 and worst_R -1.74). Those two diagnoses map to DIFFERENT
fixes — exit management vs entry selection/regime — and the trades table carried
no field that could separate them. So each cycle could only re-state the
question, which is why the proposal queue filled with restatements of it.

THE MEASUREMENT
---------------
For every closed trade, from 5-MINUTE bars inside [entry_time, exit_time]:
    mfe_r  = best  favourable excursion, in R (long: max high;  short: min low)
    mae_r  = worst adverse     excursion, in R (long: min low;   short: max high)
and, from daily bars up to the entry session:
    stop_atr = |entry - stop| / ATR(14)   (how wide the stop is, in noise units)

HOW TO READ IT
--------------
* mfe_r >= 1R on a trade that still closed negative -> the ENTRY worked and the
  EXIT gave it back. Fix the exit, not the entry.
* mfe_r < ~0.5R on most trades -> the entries have no follow-through in this
  tape. That is a selection/regime problem, not an exit problem.
* stop_atr < ~0.5 -> the stop sits inside normal noise for that instrument, so
  a 'stop-out' says little about the entry being wrong.
* capture = sum(net R) / sum(mfe_r > 0) -> of all the favourable excursion the
  market offered, how much the system actually kept.

NEVER use daily bars for this: a daily bar spans hours the trade did not exist
for and inflates BOTH mfe and mae (a 0.26%-stop trade once measured MFE +3.28R
AND MAE +3.39R off daily bars). Intraday bars from the entry moment only.

Run:
    python r_telemetry.py backfill [account]     # fill the DB columns
    python r_telemetry.py report [--out PATH]    # distribution table (markdown)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import config
import db
import indicators
from alpaca_rest import AlpacaClient, AlpacaError

# A row whose measurement failed (feed gap, symbol outside the free 5-min
# history cap) records an attempt timestamp so reconcile does not re-probe it on
# every 10-minute tick. It is retried after this many hours.
RETRY_AFTER_HOURS = 6
# 5-min bars per CALENDAR day, used to size a request. NOT 78 (a 6.5-hour regular
# session): the free IEX feed returns extended-hours bars too (observed prints at
# 08:00 and 18:55 UTC), so a day can hold far more than a session's worth. The
# estimate is deliberately an upper bound and the fetch loop re-requests if the
# page comes back exactly full, so an under-estimate cannot silently drop the
# recent tail (which is exactly how SMH/SLV/META/XLF first came back
# "unmeasurable" while holding perfectly good data).
_5MIN_PER_DAY = 192


def _ts(s: str | None) -> datetime | None:
    """Parse an ISO timestamp from Alpaca/SQLite. Tolerates a trailing 'Z'."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _z(dt: datetime) -> str:
    """RFC3339 with a trailing Z — Alpaca 400s on a naive/officeless timestamp."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def risk_per_share(t) -> float | None:
    """The trade's own planned risk unit: |entry - stop| per share."""
    e, s = t["entry_price"], t["stop_price"]
    if e in (None, "") or s in (None, ""):
        return None
    try:
        d = abs(float(e) - float(s))
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


def entry_ts(t) -> datetime | None:
    """Open timestamp. entry_time is null on rows written before Sep 17 2026 —
    created_at IS the open timestamp for those (the row is created when the
    position opens and only updated in place afterwards)."""
    return _ts(t["entry_time"]) or _ts(t["created_at"])


def _bars_in_window(client: AlpacaClient, t, t0: datetime, t1: datetime) -> list[tuple[datetime, float, float]]:
    """(timestamp, high, low) for 5-min bars inside [t0, t1].

    Two free-tier traps are handled here, both of which silently produced an
    EMPTY window for trades that had perfectly good data (verified live
    2026-09-28):
      1. `start` + `limit` returns bars ASCENDING and truncates to the FIRST
         `limit` bars of the window. A limit sized off the TRADE's duration
         (1-2 days) is far too small for the requested span (t0-3d .. t1+2d),
         so the page ended BEFORE the entry and the trade looked unmeasurable
         (SMH/SLV/META/XLF all failed this way). Size the limit off the span
         actually requested, and if the page comes back exactly full, re-request
         larger rather than trusting the arithmetic.
      2. Windows reaching today 403 on the default feed; that is handled in
         alpaca_rest.bars() (explicit feed=iex) — end is clamped to now here
         because no bar after this moment can belong to the trade.
    """
    start = (t0 - timedelta(days=2)).strftime("%Y-%m-%d")
    end_dt = min(t1, datetime.now(timezone.utc))
    end = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    span_days = max(1, (end_dt.date() - (t0 - timedelta(days=2)).date()).days + 1)
    limit = min(10000, span_days * _5MIN_PER_DAY + _5MIN_PER_DAY + 10)
    out: list[tuple[datetime, float, float]] = []
    for _ in range(3):
        try:
            data = client.bars(t["symbol"], timeframe="5Min", limit=limit,
                               start=start, end=end)
        except AlpacaError:
            return []
        got = (data or {}).get("bars") or []
        out = []
        for b in got:
            bt = _ts(b.get("t"))
            if bt is not None and t0 <= bt <= t1:
                out.append((bt, float(b["h"]), float(b["l"])))
        if out or len(got) < limit or limit >= 10000:
            return out
        limit = min(10000, limit * 3)      # page was full -> tail was dropped
    return out


def excursions(client: AlpacaClient, t, t0: datetime, t1: datetime) -> dict | None:
    """MFE/MAE in R over [t0, t1], from 5-min bars only (see module docstring)."""
    risk = risk_per_share(t)
    if not risk or t1 <= t0:
        return None
    entry = float(t["entry_price"])
    window = _bars_in_window(client, t, t0, t1)
    if not window:
        return None
    hi = max(h for _, h, _ in window)
    lo = min(l for _, _, l in window)
    if t["side"] == "long":
        mfe, mae = (hi - entry) / risk, (lo - entry) / risk
    else:
        mfe, mae = (entry - lo) / risk, (entry - hi) / risk
    return {
        "mfe_r": round(mfe, 3),
        "mae_r": round(mae, 3),
        "bars": len(window),
        "first_bar": _z(window[0][0]),
        "last_bar": _z(window[-1][0]),
    }


def stop_atr(client: AlpacaClient, t, when: datetime) -> float | None:
    """|entry - stop| / ATR(14) computed on daily bars up to the entry session."""
    risk = risk_per_share(t)
    if not risk:
        return None
    try:
        data = client.bars(t["symbol"], timeframe="1Day", limit=60,
                           start=(when - timedelta(days=120)).strftime("%Y-%m-%d"),
                           end=(when + timedelta(days=2)).strftime("%Y-%m-%d"))
    except AlpacaError:
        return None
    day = when.strftime("%Y-%m-%d")
    seq = [b for b in (data or {}).get("bars") or [] if str(b.get("t"))[:10] <= day]
    if len(seq) < 15:
        return None
    atrs = indicators.atr(seq, 14)
    atr = atrs[-1] if atrs else None
    if not atr or atr <= 0:
        return None
    return round(risk / float(atr), 3)


def measure(client: AlpacaClient, t) -> dict:
    """All telemetry for one closed trade. Missing pieces come back as None
    rather than raising — an unmeasurable row must never break reconcile."""
    t0 = entry_ts(t)
    t1 = _ts(t["exit_time"]) or _ts(t["updated_at"])
    out: dict = {"mfe_r": None, "mae_r": None, "stop_atr": None}
    if t0:
        if t1 and t1 > t0:
            ex = excursions(client, t, t0, t1)
            if ex:
                out.update(mfe_r=ex["mfe_r"], mae_r=ex["mae_r"])
        sa = stop_atr(client, t, t0)
        if sa is not None:
            out["stop_atr"] = sa
    return out


def backfill(account: str | None = None, client: AlpacaClient | None = None,
             force: bool = False, verbose: bool = True) -> int:
    """Fill mfe_r/mae_r/stop_atr for closed trades that lack them.

    Idempotent: a row that already has mfe_r is skipped, and a row whose
    measurement FAILED records an attempt timestamp (telemetry_at) so reconcile
    does not hammer the feed for it every 10 minutes.
    """
    rows = db.closed_trades_needing_telemetry(account=account,
                                              retry_after_hours=RETRY_AFTER_HOURS,
                                              force=force)
    clients: dict[str, AlpacaClient] = {}
    filled = 0
    for t in rows:
        c = client
        if c is None:
            c = clients.setdefault(t["account"], AlpacaClient(t["account"]))
        vals = measure(c, t)
        db.update_trade(t["id"], telemetry_at=datetime.now(timezone.utc).isoformat(), **vals)
        if vals["mfe_r"] is not None:
            filled += 1
            if verbose:
                print(f"[telemetry] {t['strategy']:14} {t['symbol']:6} {t['side']:5} "
                      f"mfe {vals['mfe_r']:+.2f}R mae {vals['mae_r']:+.2f}R "
                      f"stop {vals['stop_atr'] if vals['stop_atr'] is not None else '?'} ATR")
        elif verbose:
            print(f"[telemetry] {t['strategy']:14} {t['symbol']:6} unmeasurable "
                  f"(no 5-min bars for the hold window)")
    return filled


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _stats(rows: list) -> dict:
    def r_of(t):
        r = risk_per_share(t)
        if not r or t["net_pnl"] is None or not t["qty"]:
            return None
        return float(t["net_pnl"]) / (r * abs(float(t["qty"])))

    rs = [x for x in (r_of(t) for t in rows) if x is not None]
    mfes = [float(t["mfe_r"]) for t in rows if t["mfe_r"] is not None]
    maes = [float(t["mae_r"]) for t in rows if t["mae_r"] is not None]
    sats = [float(t["stop_atr"]) for t in rows if t["stop_atr"] is not None]
    # capture must compare LIKE with LIKE: realized R and offered MFE are only
    # comparable on the SAME subset of trades, i.e. the ones whose excursion was
    # actually measured. Dividing a 13-trade realized total by a 7-trade MFE
    # total mixes subsets and produces a number that means nothing.
    rs_measured = [x for x in (r_of(t) for t in rows if t["mfe_r"] is not None)
                   if x is not None]

    def med(xs):
        if not xs:
            return None
        s = sorted(xs)
        n = len(s)
        return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 3)

    total = round(sum(rs), 2) if rs else None
    avail = sum(x for x in mfes if x > 0)
    cap_kept = sum(rs_measured)
    return {
        "n": len(rows),
        "wins": sum(1 for t in rows if (t["net_pnl"] or 0) > 0),
        "total_R": total,
        "avg_R": round(total / len(rs), 3) if (rs and total is not None) else None,
        "worst_R": round(min(rs), 2) if rs else None,
        "best_R": round(max(rs), 2) if rs else None,
        "median_mfe_R": med(mfes),
        "median_mae_R": med(maes),
        "mfe_ge_1R": sum(1 for x in mfes if x >= 1.0),
        "mfe_measured": len(mfes),
        "median_stop_atr": med(sats),
        "capture": round(cap_kept / avail, 3) if avail > 0 else None,
        "capture_n": len(rs_measured),
    }


def report(out_path: str | None = None) -> str:
    db.init_db()
    trades = [t for t in db.all_trades()]
    closed = [t for t in trades if t["status"] != "open"]
    open_rows = [t for t in trades if t["status"] == "open"]

    buckets: dict[str, list] = {}
    for t in closed:
        buckets.setdefault(t["strategy"], []).append(t)

    now = datetime.now(timezone.utc)
    lines: list[str] = []
    lines.append(f"# R-multiple distribution and exit telemetry — {now:%Y-%m-%d %H:%M} UTC")
    lines.append("")
    lines.append("*Every number is program output from `r_telemetry.py report` against the live "
                 "trade DB. MFE/MAE are measured from 5-min bars INSIDE the holding window; "
                 "daily bars are never used (they inflate both — see the module docstring).*")
    lines.append("")
    lines.append(f"Closed trades: **{len(closed)}** across {len(buckets)} book(s). "
                 f"Sample gate = {config.MIN_CLOSED_TRADES_TO_TUNE} closed trades per bucket.")
    lines.append("")

    hdr = ("| book | n | wins | total R | avg R | worst R | best R | med MFE R | med MAE R | "
           "MFE>=1R | med stop (ATR) | capture |")
    lines.append(hdr)
    lines.append("|" + "---|" * 12)
    order = ["daily_orb", "mr_rsi2", "weekly_pullback", "yolo"]
    for k in [b for b in order if b in buckets] + [b for b in sorted(buckets) if b not in order]:
        s = _stats(buckets[k])
        lines.append(
            f"| {k} | {s['n']} | {s['wins']} | {s['total_R']} | {s['avg_R']} | {s['worst_R']} | "
            f"{s['best_R']} | {s['median_mfe_R']} | {s['median_mae_R']} | "
            f"{s['mfe_ge_1R']}/{s['mfe_measured']} | {s['median_stop_atr']} | {s['capture']} |")
    lines.append("")
    lines.append("**capture** = total realized R ÷ total favourable excursion (MFE) offered — how "
                 "much of the move the system kept. **MFE>=1R** counts trades whose best excursion "
                 "reached a full risk unit: a book with many MFE>=1R prints but a negative total R "
                 "is losing to its EXITS; a book with almost none is losing to its ENTRIES.")
    lines.append("")

    lines.append("## Per-trade detail (closed)")
    lines.append("")
    lines.append("| closed | book | sym | side | R | MFE R | MAE R | stop (ATR) | exit | reason |")
    lines.append("|" + "---|" * 10)
    for t in sorted(closed, key=lambda x: x["exit_time"] or x["created_at"]):
        r = risk_per_share(t)
        R = (float(t["net_pnl"]) / (r * abs(float(t["qty"])))
             if r and t["net_pnl"] is not None and t["qty"] else None)
        lines.append(
            f"| {(t['exit_time'] or '')[:16]} | {t['strategy']} | {t['symbol']} | {t['side']} | "
            f"{R:+.2f} | {t['mfe_r'] if t['mfe_r'] is not None else '—'} | "
            f"{t['mae_r'] if t['mae_r'] is not None else '—'} | "
            f"{t['stop_atr'] if t['stop_atr'] is not None else '—'} | "
            f"{(t['exit_price'] if t['exit_price'] is not None else '—')} | "
            f"{t['close_reason'] or '—'} |")
    lines.append("")

    if open_rows:
        lines.append("## Open positions (live, not persisted)")
        lines.append("")
        lines.append("| book | sym | side | opened | MFE R so far | MAE R so far | planned R:R |")
        lines.append("|" + "---|" * 7)
        c = AlpacaClient(open_rows[0]["account"])
        for t in open_rows:
            t0 = entry_ts(t)
            ex = excursions(c, t, t0, now) if t0 else None
            lines.append(
                f"| {t['strategy']} | {t['symbol']} | {t['side']} | "
                f"{str(t0)[:16] if t0 else '—'} | "
                f"{ex['mfe_r'] if ex else '—'} | {ex['mae_r'] if ex else '—'} | "
                f"{t['rr_planned'] if t['rr_planned'] is not None else '—'} |")
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("* Free IEX 5-min history is capped at roughly the last 120 calendar days, so "
                 "trades older than that are unmeasurable (they keep NULL telemetry).")
    lines.append("* IEX 5-min bars cover one venue's prints, not the consolidated tape; a thin "
                 "symbol's MFE/MAE is a lower bound on the true excursion.")
    lines.append("* The first 5-min bar can straddle the entry moment, so a trade's MFE/MAE may "
                 "include a few seconds before it existed. That flatters MFE and MAE slightly.")
    lines.append("* R here is net of fees, using the trade's own recorded stop as the risk unit — "
                 "the same definition the nightly routine and the dashboard use.")
    lines.append("")
    text = "\n".join(lines)

    if out_path:
        from pathlib import Path
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return text


def main() -> int:
    args = [a for a in sys.argv[1:]]
    cmd = args[0] if args else "report"
    if cmd == "backfill":
        db.init_db()
        acct = args[1] if len(args) > 1 else None
        n = backfill(acct)
        print(f"[telemetry] filled {n} row(s)")
        return 0
    out = None
    if "--out" in args:
        out = args[args.index("--out") + 1]
    print(report(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
