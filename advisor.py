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
    """Return {'decision','rationale','size_multiplier'}."""
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
        "max_tokens": 2000,
    }
    data = _post(body, timeout=90)

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    out = _extract_json(content)

    decision = out.get("decision", "no_go")
    if decision not in ("go", "no_go", "size_down"):
        decision = "no_go"
    try:
        size_mult = float(out.get("size_multiplier", 1.0))
    except (TypeError, ValueError):
        size_mult = 1.0
    size_mult = max(0.0, min(1.0, size_mult))

    return {"decision": decision, "rationale": out.get("rationale", ""),
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
