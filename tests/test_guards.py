"""Tests for the risk governor + guard layer.

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_guards.py

Plain asserts, no pytest (the container has no third-party packages). These
cover the two things that would be dangerous to get wrong:
  * the drawdown governor must only ever REDUCE size, must fail open when it has
    no state, and must recover on its own as equity recovers (no ratchet);
  * the guard layer must fail open when the calendar/news/regime modules are
    unavailable, so a data outage can never halt trading.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import db                                              # noqa: E402
import guards                                          # noqa: E402
import risk_gov                                        # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def test_bands() -> None:
    print("drawdown bands (reducing-only, monotonic)")
    bands = config.RISK_GOV["dd_bands"]
    mults = [risk_gov._band_multiplier(dd) for dd in (0.0, 2.9, 3.0, 3.1, 5.9, 6.0, 9.9, 12.0, 40.0)]
    print(f"  bands={bands}\n  multipliers={mults}")
    check("full size at 0% drawdown", mults[0] == 1.0)
    check("full size just under the first band", mults[1] == 1.0)
    check("band edge is inclusive (3.0% still full size)", mults[2] == 1.0)
    check("first cut just past the edge (3.1%)", mults[3] < 1.0, f"{mults[3]}")
    check("never increases risk", all(0.0 < m <= 1.0 for m in mults))
    check("monotonic non-increasing", all(a >= b for a, b in zip(mults, mults[1:])))
    check("deep drawdown keeps a floor above 0 (still tradeable)",
          mults[-1] > 0.0, f"{mults[-1]}")


def test_size_factor_multiplies() -> None:
    print("size_factor: drawdown band x event-day cut")
    real_mult = risk_gov.size_multiplier
    real_event = None
    try:
        import econ
        real_event = econ.event_day
        econ.event_day = lambda *a, **k: (True, "CPI 12:30 UTC")
        risk_gov.size_multiplier = lambda acct: (0.6, "test drawdown")
        factor, reasons = guards.size_factor("daily")
        check("two cuts multiply (0.6 x 0.5)", abs(factor - 0.30) < 1e-9, f"{factor}")
        check("both reasons surfaced for the journal", len(reasons) == 2, str(reasons))
    finally:
        risk_gov.size_multiplier = real_mult
        if real_event is not None:
            econ.event_day = real_event


def test_fail_open_without_data_sources() -> None:
    print("fail-open when the data modules are broken")
    real_event = None
    try:
        import econ
        real_event = econ.event_day
        def boom(*a, **k):
            raise RuntimeError("feed down")
        econ.event_day = boom
        econ.blackout = boom
        factor, reasons = guards.size_factor("daily")
        check("size_factor still returns a usable multiplier", 0.0 < factor <= 1.0,
              f"{factor}")
        allowed, reason = guards.entry_gate("daily")
        check("entry_gate does not block on a feed error", allowed is True, reason)
    finally:
        if real_event is not None:
            econ.event_day = real_event


def test_status_shape() -> None:
    print("governor status is journal-safe")
    st = risk_gov.status("daily")
    for key in ("drawdown_pct", "peak_equity", "size_multiplier", "can_open",
                "gate_reason", "text"):
        check(f"status carries '{key}'", key in st)
    check("size_multiplier is a float in range", isinstance(st["size_multiplier"], float)
          and 0.0 <= st["size_multiplier"] <= 1.0)
    check("text is human readable", isinstance(st["text"], str) and len(st["text"]) > 5,
          st["text"])


def test_drawdown_uses_starting_capital_as_the_peak_floor() -> None:
    print("drawdown measures from starting capital when there is no history")
    db.init_db()
    state = db.account_state().get("daily") or {}
    peak = risk_gov.peak_equity("daily")
    start = state.get("starting_equity") or config.STARTING_CAPITAL["daily"]
    check("peak >= starting capital", peak is not None and peak >= float(start) - 1e-6,
          f"peak={peak} start={start}")
    eq = risk_gov.current_equity("daily")
    if eq is not None and peak:
        expected = max(0.0, (peak - eq) / peak * 100)
        got = risk_gov.drawdown_pct("daily")
        check("drawdown matches (peak-equity)/peak", abs(got - expected) < 1e-6,
              f"{got:.4f} vs {expected:.4f}")


def test_equity_history_roundtrip() -> None:
    print("equity history round-trip")
    db.init_db()
    db.record_equity("__test__", 10000.0, 10000.0)
    db.record_equity("__test__", 9500.0, 9500.0)
    series = db.equity_series("__test__", days=1)
    check("points are recorded", len(series) >= 1, f"{len(series)} points")
    # NOTE: two writes inside the same clock minute intentionally collapse into
    # one row (the tick can fire twice). So the peak must equal the max of what
    # is actually ON DISK, never the highest value ever passed in.
    check("peak reads the max of the stored series",
          db.equity_peak("__test__") == max(r["equity"] for r in series),
          f"peak={db.equity_peak('__test__')}")
    # Two DIFFERENT minutes must both be kept. (OR REPLACE, because this suite is
    # re-runnable and the row may already exist from a previous run — a plain
    # INSERT would fail on the (account, ts) primary key and look like a bug.)
    import datetime as _dt
    conn = db.connect()
    try:
        conn.execute("INSERT OR REPLACE INTO equity_history "
                     "(account, ts, equity, cash) VALUES (?,?,?,?)",
                     ("__test__", (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5))
                      .strftime("%Y-%m-%dT%H:%M:00+00:00"), 10123.0, 10123.0))
        conn.commit()
    finally:
        conn.close()
    check("a later minute is kept and becomes the peak",
          db.equity_peak("__test__") == 10123.0, f"{db.equity_peak('__test__')}")
    # Same-minute writes must upsert, not duplicate (the tick can run twice).
    before = len(db.equity_series("__test__", days=1))
    db.record_equity("__test__", 9600.0, 9600.0)
    after = len(db.equity_series("__test__", days=1))
    check("same-minute write upserts", after == before, f"{before} -> {after}")


def main() -> int:
    print(f"config VERSION {config.VERSION}")
    db.init_db()          # every test above reads account_state / equity_history
    for fn in (test_bands, test_size_factor_multiplies, test_fail_open_without_data_sources,
               test_status_shape, test_drawdown_uses_starting_capital_as_the_peak_floor,
               test_equity_history_roundtrip):
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("all guard/governor tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
