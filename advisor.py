"""LLM decision layer — one disciplined call per candidate setup.

The deterministic rules compute the setup + risk math; the LLM gives the final
GO / NO-GO / SIZE-DOWN with news / regime / recent-performance context. This is
the "leverage LLMs, not solely TA" gate.

Fail-closed: any error, or a "no_go", means NO trade is placed.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import config

# Retry budget for a transient transport failure on the LLM call. Same class of
# bug as the Alpaca GET retry in alpaca_rest (2026-09-11): a single read timeout
# used to abort the whole decision cycle — yolo_run 2026-09-11 17:20 lost its
# entire 30-min cycle to "LLM error: The read operation timed out" (DeepSeek)
# while the alpaca_rest path already retried. The second attempt uses a shorter
# read budget so total call time stays inside the scheduler job timeout
# (app.py: daily/weekly 180s, yolo/self_improve 300s).
_RETRY_TIMEOUT = 60
_RETRY_SLEEP = 2.0

# Output-token budget for the decision call. The configured decision model
# (deepseek-flash) is a REASONING model: `reasoning_content` is billed against
# max_tokens BEFORE the JSON answer, so one budget covers both. At the old 2000
# the chain of thought consumed the whole allowance once the richer context
# layer shipped, and the answer came back EMPTY — measured live 2026-09-21:
# 3/3 calls returned finish_reason='length' with 2000/2000 tokens spent on
# reasoning and 0 chars of content. `_extract_json("")` then fell back to {},
# `decision` defaulted to 'no_go' and `rationale` to "" — so the bot silently
# skipped a valid setup and the journal recorded a bare "AI: " (the two
# mr_rsi2 XLF rows of 2026-09-17 19:33/19:48): a starvation bug wearing the
# costume of a decision. 8000 leaves room for the reasoning AND the answer
# (measured: finish_reason='stop', content 176-178 chars, ~10 s).
_DECISION_MAX_TOKENS = 8000

# Truncation recovery. A starved answer is a cut-off SPIRAL, not a verdict:
# live 2026-09-30 14:08 UTC the daily-ORB call spent all 8000 tokens on
# reasoning and returned 0 chars (finish_reason='length') while re-litigating a
# net-R:R "minimum" it had invented (the deterministic floor is 0.5 and the
# setup had already cleared it). Normal calls use ~350-430 tokens, so this is a
# rare spiral rather than a tight budget. Rather than lose the whole decision
# cycle (the bot marks itself decided and never revisits the day), hand the
# model its OWN cut-off analysis back and ask for the JSON verdict alone --
# measured live: 1.0 s, 72 tokens, finish_reason='stop', valid verdict. The
# context is unchanged, so the recovered answer is still the model's own
# decision; if it is unreadable too, the call raises exactly as before (fail
# closed AND loud). Budget is kept small so even a failed retry stays inside
# the scheduler's job timeout (app.py: daily 180s).
_RETRY_ANSWER_MAX_TOKENS = 1500
_RETRY_ANSWER_TIMEOUT = 45
_RETRY_REASONING_CHARS = 4000
_RETRY_MIN_REASONING_CHARS = 200
_RETRY_ANSWER_PROMPT = ("Your analysis above was cut off by the output token "
                        "limit. Do not continue analysing. Output ONLY the "
                        "final JSON object now.")


class AdvisorError(Exception):
    pass


def _post(body: dict, timeout: int) -> dict:
    """POST /chat/completions, retrying ONCE on a transient transport error.

    Retryable: read timeout / connection reset / DNS blip, and HTTP 5xx.
    NOT retryable: HTTP 4xx (auth or bad request — retrying cannot help).
    """
    req_data = json.dumps(body).encode()
    last_err: Exception | None = None
    attempts = (timeout, min(_RETRY_TIMEOUT, timeout))
    for attempt, budget in enumerate(attempts):
        req = urllib.request.Request(
            config.LLM_BASE_URL + "/chat/completions",
            data=req_data,
            headers={
                "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=budget) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        if attempt + 1 < len(attempts):
            time.sleep(_RETRY_SLEEP)
    raise last_err if last_err else AdvisorError("LLM request failed")


def _extract_json(content: str):
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        s, e = content.find("{"), content.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(content[s:e + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def _recover_truncated(body: dict, reasoning_text: str) -> dict:
    """Second attempt at a truncated (finish_reason='length') answer.

    Feed the model's OWN cut-off reasoning back as its previous turn and ask
    for the JSON verdict alone — so the retry answers the question the first
    call was still deliberating, instead of starting a fresh spiral. Same
    context, same system prompt: the recovered decision is still the model's.
    """
    msgs = list(body.get("messages") or [])
    msgs.append({"role": "assistant",
                 "content": reasoning_text[-_RETRY_REASONING_CHARS:]})
    msgs.append({"role": "user", "content": _RETRY_ANSWER_PROMPT})
    retry = dict(body)
    retry["messages"] = msgs
    retry["max_tokens"] = _RETRY_ANSWER_MAX_TOKENS
    return _post(retry, timeout=_RETRY_ANSWER_TIMEOUT)


def decide(context: dict, model: str | None = None) -> dict:
    """Return {'decision','rationale','size_multiplier'}.

    Fail-CLOSED and fail-LOUD: an answer that cannot be read raises
    AdvisorError (the callers journal an error event and take no trade) rather
    than coming back as a decision-shaped 'no_go' with an empty reason — an
    unreadable answer must never look like the bot deciding there was no setup.
    """
    if not config.DEEPSEEK_API_KEY:
        raise AdvisorError("No DEEPSEEK_API_KEY configured")

    model = model or config.LLM_MODEL
    system = (
        "You are a disciplined trading risk officer. Given a candidate setup, "
        "decide whether to take the trade. Use ONLY the context provided; do not "
        "invent data. Prefer NO-GO when uncertain. Respond with ONLY a JSON object "
        "(no markdown): {\"decision\": \"go\"|\"no_go\"|\"size_down\", "
        "\"rationale\": \"<one short sentence>\", \"size_multiplier\": <0.0-1.0>}."
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, indent=2)},
        ],
        "temperature": 0.2,
        "max_tokens": _DECISION_MAX_TOKENS,
    }
    data = _post(body, timeout=90)

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    reasoning_text = " ".join(str(message.get("reasoning_content") or "").split())
    out = _extract_json(content)
    retried = False
    if not str(out.get("decision") or "").strip():
        # Empty, truncated or non-JSON answer (see _DECISION_MAX_TOKENS). A
        # starved answer is a cut-off spiral rather than a verdict, so give the
        # model its own analysis back and ask for the verdict alone before
        # failing closed AND loud — an unreadable answer must never look like
        # the bot deciding there was no setup.
        if (choice.get("finish_reason") == "length"
                and len(reasoning_text) >= _RETRY_MIN_REASONING_CHARS):
            retried = True
            try:
                retry = _recover_truncated(body, reasoning_text)
            except Exception:
                retry = {}
            msg2 = (retry.get("choices") or [{}])[0].get("message") or {}
            out = _extract_json(msg2.get("content") or "")
    if not str(out.get("decision") or "").strip():
        # Fail closed, but with the diagnosis attached so the operator can tell
        # a starved/broken model call from a genuine "no setup today".
        usage = data.get("usage") or {}
        tail = " ".join(content.split())[:160] or reasoning_text[-160:]
        raise AdvisorError(
            "advisor returned no readable decision "
            f"(finish_reason={choice.get('finish_reason')}, "
            f"completion_tokens={usage.get('completion_tokens')}, "
            f"reasoning_tokens={(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')}, "
            f"content={len(content)} chars"
            + (", a verdict-only retry on its own analysis failed too" if retried else "")
            + ")" + (f"; tail: {tail}" if tail else "")
        )

    decision = str(out.get("decision")).strip().lower()
    if decision not in ("go", "no_go", "size_down"):
        decision = "no_go"
    rationale = str(out.get("rationale") or "").strip()
    if not rationale:
        # Never journal a blank reason: the decision journal is how a human
        # checks WHY a bot passed, and "AI: " explains nothing.
        rationale = "no reason given by the model"
    try:
        size_mult = float(out.get("size_multiplier", 1.0))
    except (TypeError, ValueError):
        size_mult = 1.0
    size_mult = max(0.0, min(1.0, size_mult))

    return {"decision": decision, "rationale": rationale,
            "size_multiplier": size_mult}


def chat(system: str, user: str, model: str | None = None,
         temperature: float = 0.2, max_tokens: int = 2000) -> str:
    """Single-turn chat completion returning the final assistant content.

    Works with reasoning models (e.g. deepseek-v4-pro) that emit
    `reasoning_content` before the final `content` — we return only `content`.
    """
    if not config.DEEPSEEK_API_KEY:
        raise AdvisorError("No DEEPSEEK_API_KEY configured")

    model = model or config.LLM_MODEL
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = _post(body, timeout=120)

    msg = (data.get("choices") or [{}])[0].get("message", {})
    return msg.get("content") or ""
