"""LLM decision layer — one disciplined call per candidate setup.

The deterministic rules compute the setup + risk math; the LLM gives the final
GO / NO-GO / SIZE-DOWN with news / regime / recent-performance context. This is
the "leverage LLMs, not solely TA" gate.

Fail-closed: any error, or a "no_go", means NO trade is placed.
"""
from __future__ import annotations

import json
import urllib.request

import config


class AdvisorError(Exception):
    pass


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
            return json.loads(content[s:e + 1])
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
    req = urllib.request.Request(
        config.LLM_BASE_URL + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())

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
    req = urllib.request.Request(
        config.LLM_BASE_URL + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())

    msg = (data.get("choices") or [{}])[0].get("message", {})
    return msg.get("content") or ""
