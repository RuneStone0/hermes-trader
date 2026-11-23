"""Daily 20-day-high breakout sleeve - the lab's `breakout` champion, traded as measured.

Why this sleeve exists
----------------------
The daily account's intraday sleeve (`daily_orb`) has produced **2 trades in 11
weeks** (36 no_go / 5 blocked / 5 skip): on the free data tier the ORB cannot be
validated at all, and in an ATR-percentile-100 tape every breakout it finds is
either stale or refuted. Meanwhile the strategy lab's `breakout` family is
positive in EVERY window it measures, on a whole plateau of neighbouring
parameters, and my own independent replay of the same rule on free-tier daily
bars reproduces it:

    20-day high, 1.5 ATR stop, 4.0 ATR target, 15-session hold
      2020H2-22  n=248  avgR +0.103     2025     n=124  avgR +0.226
      2023-24    n=215  avgR +0.221     2026     n= 93  avgR +0.412
      2026H2     n= 31  avgR +0.271     since Sep n= 15  avgR +0.589
    with a 4-slot concurrency cap: 2026 n=60 +0.439, 2025 +0.181,
      2023-24 +0.367, 2020H2-22 +0.109 (all positive)

The lab's grid (tested.jsonl) shows the same plateau from a different code path:
n=20 with stop 1.5/2.0/3.0 and hold 10/15/20, target 0 or 4 ATR - B-window
expectancy +0.14 to +0.28R, PF 1.5-1.8. A single lucky config does not look
like that; a real effect does.

The rule (deliberately identical to what was measured)
------------------------------------------------------
    signal : the last COMPLETED daily close > max(high of the prior 20 sessions)
    entry  : market, in the first minutes of the next session (that is the lab's
             fill convention: signal on close i, fill at open i+1)
    stop   : entry - 1.5 * ATR14(last completed bar)
    target : entry + 4.0 * ATR14(last completed bar)
    hold   : 15 sessions (~21 calendar days), then close at market

NO LLM VETO, deliberately (same reasoning as the portfolio sleeve): the lab
measured the raw rule and a veto layer would void the measurement. The old
weekly sleeve's veto turned a live signal into zero trades in its lifetime.

The one honest fidelity gap: the lab fills AT the open, this job fills in
[09:31, 09:45) ET, so the fill can be up to ~15 minutes late. Every entry logs
its distance from the session open in ATR units (`drift_atr`) so the gap stays
measurable instead of assumed harmless.

Usage: python3 breakout_run.py [--dry-run] [--assume-open]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import config
import db
import guards
import market
import wording
from alpaca_rest import AlpacaClient, AlpacaError
from indicators import atr

ACCOUNT = "daily"
STRATEGY = "daily_breakout"

PARAMS = {"lookback": 20, "stop_atr": 1.5, "tgt_atr": 4.0, "max_hold_days": 21}

UNIVERSE = ["SPY", "QQQ", "IWM", "GLD", "TLT", "XLE", "XLF", "XLV",
            "XLU", "XLP", "XLB", "XLK", "SMH", "SLV"]

MAX_POSITIONS = 4        # measured with a cap: the edge survives at 4 (and 6)
WEIGHT = 0.125           # target notional per position
MAX_GROSS = 0.50         # 4 x 12.5%; no leverage
MIN_SHARE_FRACTION = 0.25

# The lab fills at the open; act in the first quarter hour after it (ET, so DST
# is handled by the clock, not by a UTC constant).
ENTRY_WINDOW = ("09:31", "09:45")


def _today_et() -> str:
    return market.now_et().strftime("%Y-%m-%d")


def _state_path(d: str) -> Path:
    return config.STATE_DIR / f"breakout_{d}.json"


def _load_state(d: str) -> dict:
    p = _state_path(d)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(d: str, s: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(d).write_text(json.dumps(s, indent=2))


def _completed(bars: list[dict], today: str) -> list[dict]:
    """Drop today's in-progress bar: the rule is a CLOSE-based signal.

    The daily-bars endpoint can include the current session once it opens. Feeding
    it to the signal would make the rule read an unfinished close (the same class
    of error the portfolio sleeve bounds explicitly), so the filter is by the
    bar's SESSION date, not by "bars[-1]" and a hope.

    The session date is the bar's UTC date, NOT its New-York date: Alpaca stamps a
    daily bar at 00:00Z (or 04:00Z) of the trading day, which is the previous
    evening in ET - converting to ET would shift every bar back one day and make
    "today" invisible. Verified against the live feed: 1Day bars for 1590 sessions
    come back with t == "<session date>T00:00:00Z".
    """
    out = []
    for b in bars:
        ts = str(b.get("t") or b.get("timestamp") or "")
        day = ts[:10] if len(ts) >= 10 else ""
        if not day:
            try:
                day = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except Exception:
                continue
        if day < today:
            out.append(b)
    return out


def _signal(bars: list[dict]) -> dict | None:
    """close > max(high of the prior `lookback` completed sessions)."""
    n = PARAMS["lookback"]
    a = atr(bars, 14)
    if len(bars) < n + 20 or a[-1] is None or a[-1] <= 0:
        return None
    closes = [float(b["c"]) for b in bars]
    hi = max(float(b["h"]) for b in bars[-n - 1:-1])
    last = closes[-1]
    if last <= hi:
        return None
    return {"close": last, "breakout_high": hi, "atr": float(a[-1]),
            "clear_atr": (last - hi) / float(a[-1])}


def _close_past_max_hold(client: AlpacaClient, positions: dict) -> None:
    """Time stop. The bracket owns stop/target; nothing else owns 'held too long'."""
    now = datetime.now(timezone.utc)
    for t in db.open_trades(ACCOUNT):
        sym = t["symbol"]
        if sym not in positions:
            continue
        try:
            opened = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
        except Exception:
            continue
        age = (now - opened).days
        if age < PARAMS["max_hold_days"]:
            continue
        try:
            _, released = client.release_and_close(sym)
        except AlpacaError as e:
            print(f"[breakout] time-stop close failed for {sym}: {e}")
            db.log_event(ACCOUNT, STRATEGY, "error", f"time-stop close failed for {sym}: {e}")
            continue
        print(f"[breakout] TIME STOP {sym} after {age}d (released {released} leg(s))")
        db.log_event(ACCOUNT, STRATEGY, "exit",
                     wording.closed(sym, f"time stop after {age}d "
                                         f"(max hold {PARAMS['max_hold_days']}d)"),
                     detail=f"rule max_hold_days={PARAMS['max_hold_days']}, held {age} calendar days")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--assume-open", action="store_true",
                    help="TEST ONLY: skip the clock + entry-window gates. Never set in the job.")
    args = ap.parse_args()

    db.init_db()
    client = AlpacaClient(ACCOUNT)

    if not args.assume_open:
        try:
            clock = client.clock()
        except AlpacaError as e:
            print(f"[breakout] clock error: {e}")
            db.log_event(ACCOUNT, STRATEGY, "error", f"clock error: {e}", dedup=True)
            return
        if not clock.get("is_open"):
            db.log_event(ACCOUNT, STRATEGY, "skip", "market closed", dedup=True)
            return

    today = _today_et()
    st = _load_state(today)
    if st.get("decided") and not args.assume_open:
        db.log_event(ACCOUNT, STRATEGY, "skip", "already decided today", dedup=True)
        return

    hm = market.et_hm()
    if not args.assume_open and not (ENTRY_WINDOW[0] <= hm < ENTRY_WINDOW[1]):
        # Only the first minutes after the open. A frozen UTC time would be an
        # hour late through DST - the ET clock is the correct gate.
        db.log_event(ACCOUNT, STRATEGY, "skip",
                     f"outside the entry window {ENTRY_WINDOW[0]}-{ENTRY_WINDOW[1]} ET",
                     dedup=True)
        return

    positions = {p["symbol"]: p for p in client.positions()}
    _close_past_max_hold(client, positions)
    positions = {p["symbol"]: p for p in client.positions()}

    acct = client.account()
    equity = float(acct.get("equity") or 0.0)
    if equity <= 0:
        db.log_event(ACCOUNT, STRATEGY, "error", "equity unavailable")
        return
    # Slot / gross accounting counts ONLY this sleeve: the daily account also
    # carries the ORB and MR sleeves, and their positions must not be read as
    # this rule's concurrency.
    mine = [t for t in db.open_trades(ACCOUNT) if t["strategy"] == STRATEGY]
    gross = sum(abs(float(positions[t["symbol"]].get("market_value") or 0.0))
                for t in mine if t["symbol"] in positions)
    slots = MAX_POSITIONS - len([t for t in mine if t["symbol"] in positions])
    if slots <= 0:
        db.log_event(ACCOUNT, STRATEGY, "skip",
                     f"sleeve full: {MAX_POSITIONS}/{MAX_POSITIONS} positions", dedup=True)
        return

    allowed, gate_reason = guards.entry_gate(ACCOUNT)
    if not allowed:
        print(f"[breakout] blocked: {gate_reason}")
        db.log_event(ACCOUNT, STRATEGY, "blocked", f"Standing down: {gate_reason}")
        st.update(decided="blocked", note=gate_reason)
        _save_state(today, st)
        return
    fomc, fomc_why = guards.fomc_block_new_overnight()
    if fomc:
        db.log_event(ACCOUNT, STRATEGY, "blocked",
                     f"Standing down: {fomc_why} - a rate decision can gap "
                     f"straight through the stop")
        st.update(decided="blocked", note=fomc_why)
        _save_state(today, st)
        return

    size_mult, size_reasons = guards.size_factor(ACCOUNT)

    cands, near = [], []
    for sym in UNIVERSE:
        if sym in positions:
            continue
        try:
            payload = client.bars(sym, timeframe="1Day", limit=PARAMS["lookback"] + 80)
            bars = [{"t": b.get("t"), "c": float(b["c"]), "h": float(b["h"]), "l": float(b["l"])}
                    for b in (payload.get("bars") or [])]
        except AlpacaError as e:
            print(f"[breakout] {sym}: bars error {e}")
            continue
        bars = _completed(bars, today)
        sig = _signal(bars)
        if sig:
            sig["symbol"] = sym
            cands.append(sig)
        elif len(bars) > PARAMS["lookback"] + 1:
            hi = max(float(b["h"]) for b in bars[-PARAMS["lookback"] - 1:-1])
            near.append(((float(bars[-1]["c"]) - hi) / max(1e-9, hi) * 100, sym))

    if not cands:
        near.sort(reverse=True)
        closest = ", ".join(f"{s} {g:+.2f}%" for g, s in near[:3]) or "no data"
        print(f"[breakout] no 20-day-high closes across {len(UNIVERSE)} symbols; closest: {closest}")
        db.log_event(ACCOUNT, STRATEGY, "skip",
                     f"no setups: no close above its 20-day high ({len(UNIVERSE)} symbols)",
                     dedup=True)
        st.update(decided="no_setup")
        _save_state(today, st)
        return

    # Strongest breakout first when slots are scarce.
    cands.sort(key=lambda c: -c["clear_atr"])
    print(f"[breakout] {len(cands)} setup(s), {slots} slot(s), gross ${gross:,.0f} of ${equity:,.0f}")

    opened = 0
    for sig in cands:
        if slots <= 0 or gross >= equity * MAX_GROSS:
            break
        sym = sig["symbol"]
        if sym in positions:
            continue
        try:
            lt = client.latest_trade(sym)
        except AlpacaError as e:
            print(f"[breakout] {sym}: latest_trade error {e}")
            continue
        if not lt or not lt.get("price"):
            print(f"[breakout] {sym}: no live price - skipping (a bracket needs one)")
            continue
        px = float(lt["price"])
        a = sig["atr"]
        stop = round(px - PARAMS["stop_atr"] * a, 2)
        target = round(px + PARAMS["tgt_atr"] * a, 2)
        # Alpaca validates bracket legs against the LIVE price; a leg on the wrong
        # side of it is a 422, and a leg within a cent is a 422 too.
        if stop <= 0 or stop >= px * 0.997 or target <= px * 1.003:
            print(f"[breakout] {sym}: geometry rejected (px {px:.2f} stop {stop:.2f} "
                  f"target {target:.2f}) - not tradable")
            db.log_event(ACCOUNT, STRATEGY, "no_go",
                         f"{sym}: bracket geometry invalid at the live price")
            continue
        budget = min(equity * WEIGHT, equity * MAX_GROSS - gross)
        # Never plan an order the account cannot pay for. The daily ACCOUNT also
        # runs the ORB (up to ~90% notional on SPY) and the MR sleeve, so this
        # sleeve's own 50% cap says nothing about available CASH. A rejected
        # order is dashboard noise, and on an overnight hold it is a margin
        # position nobody chose.
        budget = min(budget, float(acct.get("cash") or 0.0) * 0.95)
        shares = int(budget // px)
        if shares < 1:
            if px <= equity * MIN_SHARE_FRACTION:
                print(f"[breakout] {sym}: 1 share (${px:.0f}) exceeds budget ${budget:.0f} - skipping")
            continue
        if shares * px > equity * MIN_SHARE_FRACTION:
            print(f"[breakout] {sym}: would be {shares * px / equity:.0%} of equity in one name - skipping")
            continue
        if size_mult < 1.0:
            shares = max(1, int(shares * size_mult))
            guards.note_size_adjust(ACCOUNT, STRATEGY, size_mult, size_reasons)

        drift_atr = None
        try:
            # Today's OPEN, from 5-minute bars: a 1Day bar for the current session
            # may not exist yet mid-session, and a missing drift must read as
            # "unknown" (None), not as 0.00 - a zero would look like a perfect
            # fill and hide the one fidelity gap this sleeve has.
            day_bars = client.bars(sym, timeframe="5Min", limit=10,
                                   start=today).get("bars") or []
            if day_bars:
                drift_atr = (px - float(day_bars[0]["o"])) / a
        except AlpacaError:
            pass
        drift_txt = "unknown" if drift_atr is None else f"{drift_atr:+.2f} ATR"

        notional = shares * px
        print(f"[breakout] BUY {sym} x{shares} @~{px:.2f}  stop={stop:.2f} target={target:.2f} "
              f"notional=${notional:,.0f} ({notional / equity:.1%})  "
              f"breakout +{sig['clear_atr']:.2f} ATR  drift {drift_txt}  "
              f"atr={a:.2f}")
        if args.dry_run:
            opened += 1
            gross += notional
            slots -= 1
            continue
        try:
            order = client.bracket_order(sym, shares, "buy", stop_price=stop,
                                         target_price=target, entry_type="market",
                                         time_in_force="gtc")
        except AlpacaError as e:
            print(f"[breakout] {sym}: order error {e}")
            db.log_event(ACCOUNT, STRATEGY, "error", f"{sym}: order error {e}")
            continue
        db.insert_trade(
            account=ACCOUNT, strategy=STRATEGY, symbol=sym, asset_class="etf",
            side="long", qty=shares, entry_price=px, status="open",
            rr_planned=PARAMS["tgt_atr"] / PARAMS["stop_atr"],
            stop_price=stop, target_price=target,
            order_id=order.get("id"), client_order_id=order.get("client_order_id"),
            note=(f"rule: last close {sig['close']:.2f} > 20-day high {sig['breakout_high']:.2f} "
                  f"(+{sig['clear_atr']:.2f} ATR); ATR14 {a:.2f}; fill drift {drift_txt}"),
            stop_atr=PARAMS["stop_atr"])
        db.log_event(ACCOUNT, STRATEGY, "go", wording.opened(sym, "long", shares, size_mult),
                     detail=(f"20-day-high breakout: close {sig['close']:.2f} > high "
                             f"{sig['breakout_high']:.2f} (+{sig['clear_atr']:.2f} ATR); "
                             f"stop {stop:.2f} target {target:.2f}; "
                             f"{notional / equity:.1%} of equity"))
        opened += 1
        gross += notional
        slots -= 1

    if opened == 0:
        db.log_event(ACCOUNT, STRATEGY, "skip", f"{len(cands)} setup(s) but none sized/slotted",
                     dedup=True)
    st.update(decided="opened" if opened else "no_size", opened=opened)
    _save_state(today, st)
    print(f"[breakout] opened {opened} position(s); gross now ${gross:,.0f} of ${equity:,.0f}")


if __name__ == "__main__":
    main()
