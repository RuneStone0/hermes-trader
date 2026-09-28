"""Tests for the nightly review's loop-breaker (`settled_findings`).

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_settled_findings.py

Plain asserts, no pytest (the container has no third-party packages).

The pathology this pins (observed nightly 2026-09-11..25): the self-improve routine
was given the tail of lessons_learned.md and strategy_proposals.md, so it kept
re-deriving the SAME proposals from its own stale notes — the ATR-stop guard, the
sector-RS entry gate and the "config drift" item were re-filed unchanged, night
after night, some of them already implemented in code and one of them based on
misreading the tuner's bounds as a live state. The fix is to hand the routine an
explicit list of settled questions; the failure mode of that fix is SILENT (if the
file is delivered truncated from the wrong end, or is not delivered at all, the
routine just starts re-filing again and nothing errors).
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import self_improve                                    # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def _payload(settled_path: Path) -> dict:
    real = config.CLOSED_PROPOSALS_PATH
    config.CLOSED_PROPOSALS_PATH = settled_path
    try:
        return self_improve.review_payload({"books": []}, {"regime": "trend_up"}, [])
    finally:
        config.CLOSED_PROPOSALS_PATH = real


def test_settled_findings_key_is_always_present() -> None:
    print("payload always carries the key, even when the file is missing")
    missing = Path("/nonexistent/settled_findings.md")
    p = _payload(missing)
    check("key present", "settled_findings" in p, f"keys={sorted(p)}")
    check("empty when absent (never raises)", p["settled_findings"] == "",
          f"value={p['settled_findings']!r}")
    check("the other review inputs are still there",
          {"performance", "market_context", "existing_lessons", "tunable_knobs"} <= set(p),
          f"keys={sorted(p)}")


def test_short_file_is_delivered_whole() -> None:
    print("a short settled file is delivered WHOLE, not tail-truncated")
    tmp = Path("/tmp/_settled_short.md")
    tmp.write_text("HEAD-VERDICT do not re-propose the ATR stop guard\n" + ("filler\n" * 5))
    p = _payload(tmp)
    check("head verdict survives", "HEAD-VERDICT" in p["settled_findings"],
          f"len={len(p['settled_findings'])}")


def test_long_file_is_bounded_from_the_head() -> None:
    print("an over-long settled file is bounded, keeping the NEWEST (top) entries")
    tmp = Path("/tmp/_settled_long.md")
    tmp.write_text("NEWEST-VERDICT\n" + ("x" * 5000 + "\n"))
    p = _payload(tmp)
    check("bounded to 3000 chars", len(p["settled_findings"]) == 3000,
          f"len={len(p['settled_findings'])}")
    check("newest entry kept (truncated from the TAIL, not the head)",
          p["settled_findings"].startswith("NEWEST-VERDICT"),
          f"start={p['settled_findings'][:24]!r}")


def test_real_file_carries_live_verdicts() -> None:
    print("the REAL settled-findings file carries the two 2026-09-28 verdicts")
    real = config.CLOSED_PROPOSALS_PATH
    check("file exists", real.exists(), str(real))
    if not real.exists():
        return
    text = real.read_text()
    p = _payload(real)
    for marker in ("FIXED IN CODE 2026-09-17", "NOT SUPPORTED", "NOT DRIFT"):
        check(f"verdict '{marker}' present in the file", marker in text)
    check("both 2026-09-28 verdicts survive the 3000-char bound",
          "FIXED IN CODE 2026-09-17" in p["settled_findings"]
          and "NOT SUPPORTED" in p["settled_findings"],
          f"len={len(p['settled_findings'])}")


def test_system_prompt_forbids_re_filing() -> None:
    print("the system prompt actually tells the routine not to re-file settled items")
    src = inspect.getsource(self_improve.main)
    check("prompt references settled_findings", "settled_findings" in src)
    check("prompt forbids re-proposing", "Do NOT re-propose" in src)
    check("prompt demands a falsifier", "falsifier" in src)


def main() -> None:
    for fn in (test_settled_findings_key_is_always_present,
               test_short_file_is_delivered_whole,
               test_long_file_is_bounded_from_the_head,
               test_real_file_carries_live_verdicts,
               test_system_prompt_forbids_re_filing):
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        sys.exit(1)
    print("all settled-findings checks passed")


if __name__ == "__main__":
    main()
