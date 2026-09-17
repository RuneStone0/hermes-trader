"""Tests for the strategy-level rules that were changed on 2026-09-17.

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_strategies.py

These pin behaviour that is easy to break by accident and expensive to get wrong:
  * the opening-range breakout must refuse a STALE breakout (the old code would
    enter a 10:05 breakout at 15:30, several ranges past its own stop);
  * the LLM's anti-doom-loop framing must actually say the things that stop it
    from refusing to trade (it does not get to be vague about it);
  * the fee/R:R gate still rejects a setup whose edge is eaten by costs.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import daily_run                                       # noqa: E402
import db                                              # noqa: E402
import fees                                            # noqa: E402
import guards                                          # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def _bar(hm_utc: str, o: float, h: float, l: float, c: float, v: int = 1000) -> dict:
    """A 5-min bar at HH:MM UTC on a fixed date (14:00Z = 10:00 ET in EDT)."""
    return {"t": f"2026-09-17T{hm_utc}:00Z", "o": o, "h": h, "l": l, "c": c, "v": v}


# The opening range (13:30-14:00Z = 09:30-10:00 ET): high 100, low 99, range 1.
ORB = [_bar("13:30", 99.2, 99.6, 99.1, 99.5),
       _bar("13:35", 99.5, 100.0, 99.4, 99.8),
       _bar("13:40", 99.8, 99.9, 99.0, 99.3),
       _bar("13:45", 99.3, 99.5, 99.0, 99.2)]


def test_no_breakout_returns_none() -> None:
    print("inside the range => no setup")
    post = [_bar("14:05", 99.4, 99.8, 99.2, 99.6), _bar("14:10", 99.6, 99.9, 99.4, 99.7)]
    check("no breakout -> None", daily_run._orb_setup(ORB + post, 10000.0) is None)


def test_fresh_long_breakout() -> None:
    print("fresh breakout above the range => long, stop at the range low")
    post = [_bar("14:05", 99.9, 100.4, 99.8, 100.2)]
    s = daily_run._orb_setup(ORB + post, 10000.0)
    check("setup found", s is not None)
    if not s:
        return
    check("side long", s["side"] == "long")
    check("entry at the range high", abs(s["entry"] - 100.0) < 1e-9, str(s["entry"]))
    check("stop at the range low", abs(s["stop"] - 99.0) < 1e-9, str(s["stop"]))
    check("target = entry + rr_multiple x range",
          abs(s["target"] - (100.0 + config.DAILY["rr_multiple"] * 1.0)) < 1e-9, str(s["target"]))
    # The ORB sizes off DAILY['risk_pct'] (0.5%), NOT the global 1% default: the
    # backtest showed no demonstrated edge for the ORB, so it runs at half the
    # risk of the mean-reversion sleeve that does have evidence.
    expect = max(1, int(10000.0 * config.DAILY["risk_pct"] / 1.0))
    check("risk sizing = DAILY risk% x equity / range",
          s["shares"] == expect, f"{s['shares']} vs {expect}")
    check("ORB risk is below the global default",
          config.DAILY["risk_pct"] < config.RISK_PCT_PER_TRADE,
          f"{config.DAILY['risk_pct']} < {config.RISK_PCT_PER_TRADE}")
    check("not stale when price is just past the edge", s["stale"] is False,
          f"chase {s['chase_pct']:.3f}%")


def test_stale_breakout_is_flagged() -> None:
    print("breakout found long after the fact => STALE (no chase)")
    # Same range, but the tape is now 1% above the edge (a 15:30 fill of a 10:05
    # breakout). Realised risk would be ~2x the plan: entry 101 vs stop 99.
    post = [_bar("14:05", 99.9, 100.4, 99.8, 100.2),
            _bar("19:55", 100.9, 101.2, 100.8, 101.0)]
    s = daily_run._orb_setup(ORB + post, 10000.0)
    check("setup found", s is not None)
    if not s:
        return
    check("flagged stale", s["stale"] is True, f"chase {s['chase_pct']:.3f}%")
    check("chase measured from the range edge", abs(s["chase_pct"] - 1.0) < 1e-6,
          f"{s['chase_pct']:.4f}")
    limit = float(config.DAILY["max_chase_pct"])
    check("limit is a real guard (1.0% > configured limit)", 1.0 > limit, f"limit {limit}%")


def test_short_breakout_and_staleness_sign() -> None:
    print("breakout BELOW the range => short, and the chase sign is correct")
    post = [_bar("14:05", 99.1, 99.2, 98.7, 98.8)]
    s = daily_run._orb_setup(ORB + post, 10000.0)
    check("setup found", s is not None)
    if not s:
        return
    check("side short", s["side"] == "short")
    check("entry at the range low", abs(s["entry"] - 99.0) < 1e-9, str(s["entry"]))
    check("stop at the range high", abs(s["stop"] - 100.0) < 1e-9, str(s["stop"]))
    check("target below entry", s["target"] < s["entry"], str(s["target"]))
    check("fresh short is not stale", s["stale"] is False, f"chase {s['chase_pct']:.3f}%")
    # ... and a short that has ALREADY fallen 1% past the edge is stale too.
    post2 = post + [_bar("19:55", 98.2, 98.3, 98.0, 98.05)]
    s2 = daily_run._orb_setup(ORB + post2, 10000.0)
    check("deep short is stale", s2 is not None and s2["stale"] is True,
          f"{(s2 or {}).get('chase_pct')}")


def test_wick_mode() -> None:
    print("close_confirmation=False uses wicks")
    real = config.DAILY.get("close_confirmation")
    try:
        config.DAILY["close_confirmation"] = False
        post = [_bar("14:05", 99.6, 100.3, 99.5, 99.7)]   # wick above, close inside
        s = daily_run._orb_setup(ORB + post, 10000.0)
        check("wick counts as a breakout when confirmation is off", s is not None)
        if s:
            check("wick breakout risk > 0", s["range"] > 0, str(s["range"]))
    finally:
        config.DAILY["close_confirmation"] = real


def test_fee_gate_rejects_a_dead_setup() -> None:
    print("fee-adjusted R:R gate")
    # Reward 0.2 against 1.0 of risk -> gross R:R of 0.2, well under the 0.5
    # MIN_NET_RR floor once fees are deducted.
    rr = fees.fee_adjusted_rr(100.0, 100.2, 99.0, "long", 10)
    check("a setup risking $1.00 to make $0.20 is rejected", rr["clears"] is False,
          f"gross_rr={rr.get('gross_rr')} net_rr={rr.get('net_rr')}")
    rr2 = fees.fee_adjusted_rr(100.0, 103.0, 99.0, "long", 100)
    check("a 3:1 setup clears", rr2["clears"] is True, f"net_rr={rr2.get('net_rr')}")


def test_framing_teaches_variance_not_panic() -> None:
    print("anti-doom-loop framing after a losing streak")
    db.init_db()
    acct = "__frame_test__"
    conn = db.connect()
    try:
        conn.execute("DELETE FROM trades WHERE account=?", (acct,))
        for i in range(7):
            conn.execute(
                "INSERT INTO trades (account,strategy,symbol,asset_class,side,qty,"
                "entry_price,exit_price,status,gross_pnl,fees,net_pnl,stop_price,"
                "target_price,close_reason,created_at,updated_at,entry_time,exit_time) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (acct, "yolo", "GLD", "etf", "long", 10, 100.0, 99.0, "closed",
                 -10.0, 0.05, -10.0, 99.0, 103.0, "stop", "2026-09-10T14:00:00Z",
                 "2026-09-10T15:00:00Z", "2026-09-10T14:00:00Z", "2026-09-10T15:00:00Z"))
        conn.commit()
    finally:
        conn.close()
    note = guards.framing(acct)
    check("framing is produced", bool(note))
    check("it states the sample as W/L", "0W/7L" in note, note[:60])
    check("it teaches that a streak is variance, not proof",
          "variance" in note or "6% of the time" in note)
    check("it explicitly discourages refusing to trade",
          "refuse to trade" in note or "Do NOT" in note)
    check("it locates the defect (entry timing / stop distance) when all exits were stops",
          "ENTRY TIMING" in note, note[-120:])
    # A healthy sample must NOT be framed as a catastrophe.
    conn = db.connect()
    try:
        conn.execute("DELETE FROM trades WHERE account=?", (acct,))
        conn.execute(
            "INSERT INTO trades (account,strategy,symbol,asset_class,side,qty,entry_price,"
            "exit_price,status,gross_pnl,fees,net_pnl,close_reason,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (acct, "yolo", "SPY", "etf", "long", 10, 100.0, 102.0, "closed",
             20.0, 0.05, 20.0, "target", "2026-09-10T14:00:00Z", "2026-09-10T15:00:00Z"))
        conn.commit()
    finally:
        conn.close()
    note2 = guards.framing(acct)
    check("a winning sample is not told the entry timing is broken",
          "ENTRY TIMING" not in note2, note2[:80])


def main() -> int:
    print(f"config VERSION {config.VERSION}")
    db.init_db()
    for fn in (test_no_breakout_returns_none, test_fresh_long_breakout,
               test_stale_breakout_is_flagged, test_short_breakout_and_staleness_sign,
               test_wick_mode, test_fee_gate_rejects_a_dead_setup,
               test_framing_teaches_variance_not_panic):
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("all strategy tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
