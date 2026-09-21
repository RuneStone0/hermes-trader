"""Tests for the LLM decision layer's failure modes (advisor.decide).

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_advisor.py

Why this file exists: the decision model (deepseek-flash) is a REASONING model,
so its `reasoning_content` is billed against the SAME max_tokens budget as the
JSON answer. With the old 2000-token budget the chain of thought ate the whole
allowance and the answer came back EMPTY; `_extract_json("")` fell back to {},
`decision` defaulted to 'no_go' and `rationale` to "" — the bot skipped a valid
setup and the journal recorded a bare "AI: " (mr_rsi2 XLF, 2026-09-17
19:33/19:48). A starvation bug must never again be able to wear the costume of a
decision. These tests pin:
  * an unreadable answer RAISES (fail closed AND loud), it does not return a
    decision-shaped no_go;
  * the request asks for a budget big enough to hold reasoning PLUS the answer;
  * a returned rationale is never blank.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import advisor                                          # noqa: E402
import config                                           # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


SEEN_BODY: dict = {}


def _stub(response: dict):
    def _fake_post(body: dict, timeout: int) -> dict:
        SEEN_BODY.clear()
        SEEN_BODY.update(body)
        return response
    return _fake_post


def _resp(content: str, *, finish: str = "stop", completion: int = 100,
          reasoning: int = 0, reasoning_text: str = "") -> dict:
    msg = {"content": content}
    if reasoning_text:
        msg["reasoning_content"] = reasoning_text
    return {
        "choices": [{"finish_reason": finish, "message": msg}],
        "usage": {"completion_tokens": completion,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}},
    }


STARVED = _resp("", finish="length", completion=2000, reasoning=2000,
                reasoning_text="Weighing the setup against the tape; the breadth " * 20)


def test_starved_answer_raises_not_no_go() -> None:
    orig, orig_key = advisor._post, config.DEEPSEEK_API_KEY
    config.DEEPSEEK_API_KEY = "test-key"
    advisor._post = _stub(STARVED)
    try:
        advisor.decide({"symbol": "XLF"})
        check("a starved (empty-content) answer raises AdvisorError", False,
              "decide() returned instead of raising")
    except advisor.AdvisorError as e:
        text = str(e)
        check("a starved (empty-content) answer raises AdvisorError", True, text[:90])
        check("the error carries the diagnosis (finish_reason)", "finish_reason=length" in text)
        check("the error is not a silent 'no_go'", "no_go" not in text.split("tail")[0])
    except Exception as e:                                   # pragma: no cover
        check("a starved (empty-content) answer raises AdvisorError", False,
              f"raised {type(e).__name__}")
    finally:
        advisor._post, config.DEEPSEEK_API_KEY = orig, orig_key


def test_non_json_answer_raises() -> None:
    orig, orig_key = advisor._post, config.DEEPSEEK_API_KEY
    config.DEEPSEEK_API_KEY = "test-key"
    advisor._post = _stub(_resp("I am sorry, I cannot answer that in JSON."))
    try:
        advisor.decide({"symbol": "XLF"})
        check("a non-JSON answer raises instead of defaulting to no_go", False)
    except advisor.AdvisorError:
        check("a non-JSON answer raises instead of defaulting to no_go", True)
    finally:
        advisor._post, config.DEEPSEEK_API_KEY = orig, orig_key


def test_missing_decision_key_raises() -> None:
    orig, orig_key = advisor._post, config.DEEPSEEK_API_KEY
    config.DEEPSEEK_API_KEY = "test-key"
    advisor._post = _stub(_resp('{"rationale": "looked fine", "size_multiplier": 1.0}'))
    try:
        advisor.decide({"symbol": "XLF"})
        check("JSON without a decision key is treated as unreadable", False)
    except advisor.AdvisorError:
        check("JSON without a decision key is treated as unreadable", True)
    finally:
        advisor._post, config.DEEPSEEK_API_KEY = orig, orig_key


def test_token_budget_covers_reasoning_plus_answer() -> None:
    orig, orig_key = advisor._post, config.DEEPSEEK_API_KEY
    config.DEEPSEEK_API_KEY = "test-key"
    advisor._post = _stub(_resp('{"decision": "go", "rationale": "clean setup", "size_multiplier": 1.0}'))
    try:
        advisor.decide({"symbol": "XLF"})
        got = SEEN_BODY.get("max_tokens")
        check("the decision call asks for a reasoning-sized budget", (got or 0) >= 4000, f"max_tokens={got}")
    finally:
        advisor._post, config.DEEPSEEK_API_KEY = orig, orig_key


def test_go_and_no_go_round_trip_with_a_reason() -> None:
    orig, orig_key = advisor._post, config.DEEPSEEK_API_KEY
    config.DEEPSEEK_API_KEY = "test-key"
    try:
        advisor._post = _stub(_resp('{"decision": "go", "rationale": "oversold in an uptrend", "size_multiplier": 0.5}'))
        out = advisor.decide({"symbol": "XLF"})
        check("a go is passed through with its size multiplier",
              out["decision"] == "go" and out["size_multiplier"] == 0.5 and out["rationale"] == "oversold in an uptrend",
              str(out))
        advisor._post = _stub(_resp('{"decision": "no_go"}'))
        out = advisor.decide({"symbol": "XLF"})
        check("a no_go without a rationale still gets a readable reason",
              out["decision"] == "no_go" and len(out["rationale"]) > 3, repr(out["rationale"]))
        advisor._post = _stub(_resp('{"decision": "SIZE_DOWN", "rationale": "narrow breadth"}'))
        out = advisor.decide({"symbol": "XLF"})
        check("the decision is normalised to lower case", out["decision"] == "size_down", str(out))
    finally:
        advisor._post, config.DEEPSEEK_API_KEY = orig, orig_key


def main() -> int:
    print(f"config VERSION {config.VERSION}")
    for fn in (test_starved_answer_raises_not_no_go, test_non_json_answer_raises,
               test_missing_decision_key_raises, test_token_budget_covers_reasoning_plus_answer,
               test_go_and_no_go_round_trip_with_a_reason):
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("all advisor tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
