"""Tests for the nightly review's ANSWER HANDLING (starvation, not silence).

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_self_improve.py

Plain asserts, no pytest (the container has no third-party packages).

The pathology this pins (measured live in-container 2026-10-19): `advisor.chat`
returned only `content`, so the routine could not tell a genuine "nothing to say"
from a call whose whole budget went to reasoning. On the real payload,
max_tokens=4000 gave finish_reason='length', 2,559 reasoning tokens and content
truncated mid-JSON at 5,235 chars; `advisor._extract_json` turned that into {},
`isinstance(out, dict)` passed, and the routine printed "appended 0 lessons;
Auto-tuned 0 setting(s); 0 structural proposal(s)" — indistinguishable from a
healthy no-op — for 14+ consecutive nights. At max_tokens=8000 the SAME payload
returns 8 lessons and 4 proposals. The fix has three parts, all pinned here:
  1. budget for reasoning PLUS the answer,
  2. an unreadable answer is an ERROR event, never "0 lessons",
  3. a genuine empty answer is journaled as its own visible outcome.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import advisor                                          # noqa: E402
import config                                           # noqa: E402
import db                                               # noqa: E402
import self_improve                                     # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def test_budget_covers_reasoning_plus_answer() -> None:
    print("the review's token budget covers reasoning + answer, not just the answer")
    # Measured: the 8000-token call used 5,253 reasoning tokens and returned
    # 6,812 chars of content (finish_reason='stop'). 4000 truncated it.
    check("budget >= 8000 (measured completion need was 7,125)",
          self_improve._REVIEW_MAX_TOKENS >= 8000,
          f"_REVIEW_MAX_TOKENS={self_improve._REVIEW_MAX_TOKENS}")
    check("retry budget is small enough to stay inside the 300s job timeout",
          0 < self_improve._RETRY_MAX_TOKENS <= 6000,
          f"_RETRY_MAX_TOKENS={self_improve._RETRY_MAX_TOKENS}")
    # Timing basis (measured 2026-10-28, in-container, real payload): a full
    # 20,000-token review call returned finish='stop' in 39.8 s. A 4,000-token
    # retry is a fraction of that, so the 300 s scheduler timeout is not at risk.


def test_parse_review_rejects_truncated_and_empty() -> None:
    print("an unreadable answer is None (an error), never an empty review")
    truncated = '{"lessons":[{"topic":"Judge DAILY by R","detail":"net +$3.65 but'
    check("truncated JSON -> None", self_improve.parse_review(truncated) is None,
          f"got={self_improve.parse_review(truncated)!r}")
    check("empty content -> None", self_improve.parse_review("") is None)
    check("prose with no JSON -> None",
          self_improve.parse_review("I reviewed the book and it looks fine.") is None)
    check("{} -> None (no keys at all)", self_improve.parse_review("{}") is None)
    check("a dict without the two keys -> None",
          self_improve.parse_review('{"summary":"ok"}') is None)


def test_parse_review_accepts_a_genuine_empty_answer() -> None:
    print("a genuine 'nothing new' answer parses to two EMPTY lists (a visible no-op)")
    out = self_improve.parse_review('{"lessons":[],"proposals":[]}')
    check("parsed", isinstance(out, dict), f"got={out!r}")
    check("both keys normalised to lists",
          isinstance(out, dict) and out["lessons"] == [] and out["proposals"] == [])


def test_parse_review_drops_non_dict_entries() -> None:
    print("junk entries are dropped rather than crashing the file append")
    out = self_improve.parse_review(
        '{"lessons":["just a string",{"topic":"t","detail":"d"}],"proposals":null}')
    check("kept the dict lesson", isinstance(out, dict) and len(out["lessons"]) == 1,
          f"got={out!r}")
    check("null proposals -> []", isinstance(out, dict) and out["proposals"] == [])


def test_recoverable_detects_a_cut_off_answer() -> None:
    print("_recoverable spots a cut-off answer (and leaves a real one alone)")
    check("finish_reason=length -> recoverable",
          self_improve._recoverable({"finish_reason": "length", "content": "x", "reasoning": ""}))
    check("empty content + long reasoning -> recoverable",
          self_improve._recoverable({"finish_reason": "stop", "content": "",
                                     "reasoning": "x" * 300}))
    check("a real answer -> not recoverable",
          not self_improve._recoverable({"finish_reason": "stop",
                                         "content": '{"lessons":[]}', "reasoning": "x" * 500}))


class _Harness:
    """Run main() fully offline: no broker, no LLM, no writes outside tmp."""

    def __init__(self, replies: list[dict]):
        self.replies = replies
        self.events: list[tuple] = []
        self.calls: list[dict] = []

    def __enter__(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="si_test_")
        self._saved = {
            "perf": self_improve._perf_summary,
            "ctx": self_improve._market_context,
            "knobs": config.tunable_knobs,
            "chat_full": advisor.chat_full,
            "log_event": db.log_event,
            "init_db": db.init_db,
            "lessons": config.LESSONS_PATH,
            "proposals": config.PROPOSALS_PATH,
            "closed": config.CLOSED_PROPOSALS_PATH,
        }
        self.lessons = Path(self.tmp) / "lessons_learned.md"
        self.proposals = Path(self.tmp) / "strategy_proposals.md"

        def fake_chat_full(system, user, **kw):
            self.calls.append({"max_tokens": kw.get("max_tokens"),
                               "extra_messages": kw.get("extra_messages")})
            return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]

        self_improve._perf_summary = lambda: {a: {"closed": 3} for a in self_improve.ACCOUNTS}
        self_improve._market_context = lambda: {"regime": "chop"}
        config.tunable_knobs = lambda: []
        advisor.chat_full = fake_chat_full
        db.log_event = lambda acct, strat, dec, reason="", detail=None, dedup=False: (
            self.events.append((acct, strat, dec, reason)) or 0)
        db.init_db = lambda: None
        config.LESSONS_PATH = self.lessons
        config.PROPOSALS_PATH = self.proposals
        config.CLOSED_PROPOSALS_PATH = Path(self.tmp) / "closed.md"
        return self

    def __exit__(self, *exc):
        self_improve._perf_summary = self._saved["perf"]
        self_improve._market_context = self._saved["ctx"]
        config.tunable_knobs = self._saved["knobs"]
        advisor.chat_full = self._saved["chat_full"]
        db.log_event = self._saved["log_event"]
        db.init_db = self._saved["init_db"]
        config.LESSONS_PATH = self._saved["lessons"]
        config.PROPOSALS_PATH = self._saved["proposals"]
        config.CLOSED_PROPOSALS_PATH = self._saved["closed"]
        return False


def test_main_journals_an_error_on_an_unreadable_answer() -> None:
    print("main(): an unreadable answer journals an ERROR (not '0 lessons')")
    truncated = {"content": '{"lessons":[{"topic":"a","detail":"b"},',
                 "reasoning": "x" * 500, "finish_reason": "length", "usage": {}}
    with _Harness([truncated, {"content": "", "reasoning": "y" * 500,
                               "finish_reason": "length", "usage": {}}]) as h:
        self_improve.main()
        check("two calls (initial + one recovery retry)", len(h.calls) == 2,
              f"calls={len(h.calls)}")
        check("recovery retry carries the model's own cut-off turn",
              bool(h.calls[1]["extra_messages"]) if len(h.calls) > 1 else False)
        kinds = [e[2] for e in h.events]
        check("journaled an error", kinds.count("error") == 1, f"events={kinds}")
        check("did NOT journal a no-op", "no-op" not in kinds, f"events={kinds}")
        check("wrote no lessons file", not h.lessons.exists())
        check("wrote no proposals file", not h.proposals.exists())


def test_main_journals_no_op_for_a_genuine_empty_answer() -> None:
    print("main(): a genuine empty answer is a VISIBLE no-op")
    empty = {"content": '{"lessons":[],"proposals":[]}', "reasoning": "short",
             "finish_reason": "stop", "usage": {}}
    with _Harness([empty]) as h:
        self_improve.main()
        kinds = [e[2] for e in h.events]
        check("journaled a no-op", kinds == ["no-op"], f"events={kinds}")
        check("no error journaled", "error" not in kinds, f"events={kinds}")
        check("wrote no lessons file", not h.lessons.exists())


def test_main_writes_lessons_and_proposals_when_readable() -> None:
    print("main(): a readable answer is written to the volume files")
    ok = {"content": ('{"lessons":[{"topic":"Entry quality","detail":"8 of 23 reached +1R MFE"}],'
                      '"proposals":[{"title":"Normalise risk per trade","rationale":"x","params":{}}]}'),
          "reasoning": "short", "finish_reason": "stop", "usage": {}}
    with _Harness([ok]) as h:
        self_improve.main()
        check("lessons appended", h.lessons.exists() and "Entry quality" in h.lessons.read_text(),
              str(h.lessons))
        check("structural proposal queued",
              h.proposals.exists() and "Normalise risk per trade" in h.proposals.read_text(),
              str(h.proposals))
        check("no error journaled", all(e[2] != "error" for e in h.events), f"events={h.events}")


def main() -> None:
    for fn in (test_budget_covers_reasoning_plus_answer,
               test_parse_review_rejects_truncated_and_empty,
               test_parse_review_accepts_a_genuine_empty_answer,
               test_parse_review_drops_non_dict_entries,
               test_recoverable_detects_a_cut_off_answer,
               test_main_journals_an_error_on_an_unreadable_answer,
               test_main_journals_no_op_for_a_genuine_empty_answer,
               test_main_writes_lessons_and_proposals_when_readable):
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        sys.exit(1)
    print("all self-improve answer-handling checks passed")


if __name__ == "__main__":
    main()
