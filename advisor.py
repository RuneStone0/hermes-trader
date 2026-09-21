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
    out = _extract_json(content)
    if not str(out.get("decision") or "").strip():
        # Empty, truncated or non-JSON answer (see _DECISION_MAX_TOKENS). Fail
        # closed, but with the diagnosis attached so the operator can tell a
        # starved/broken model call from a genuine "no setup today".
        usage = data.get("usage") or {}
        tail = " ".join(content.split())[:160]
        if not tail:
            tail = " ".join(str(message.get("reasoning_content") or "").split())[-160:]
        raise AdvisorError(
            "advisor returned no readable decision "
            f"(finish_reason={choice.get('finish_reason')}, "
            f"completion_tokens={usage.get('completion_tokens')}, "
            f"reasoning_tokens={(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')}, "
            f"content={len(content)} chars)" + (f"; tail: {tail}" if tail else "")
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
