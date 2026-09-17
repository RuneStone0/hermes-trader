"""Alpaca's news feed — free on the paper account, and currently unused.

Every bot's LLM context used to carry a hardcoded
    "news": "no news feed (requires Alpaca data subscription)"
which is simply untrue: `/v1beta1/news` on the market-data host serves Benzinga
headlines WITH symbol tags and a one-paragraph summary, on the same keys the
bots already use for bars. Verified live 2026-09-17 (returned headlines tagged
with META, GOOGL, MSFT...). The daily bot's advisor prompt was even asking for
"news confirmation" of a breakout while being told no news feed existed.

So: symbol-tagged headlines + summaries, compacted into a short digest the
decision layer can actually read. Fail-open everywhere — a news outage must
never stop a trade, it just means the context is thinner.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

CACHE_TTL_S = 900          # 15 min: news is not a tick-level input
_MAX_HEADLINE = 110
_MAX_SUMMARY = 180


def _cache_get(key: str):
    return CACHE.get(key)


class _Cache:
    def __init__(self):
        self.data: dict = {}

    def get(self, key, ttl=CACHE_TTL_S):
        hit = self.data.get(key)
        if not hit:
            return None
        ts, val = hit
        if (datetime.now(timezone.utc) - ts).total_seconds() > ttl:
            return None
        return val

    def put(self, key, val):
        self.data[key] = (datetime.now(timezone.utc), val)
        return val


CACHE = _Cache()


def _client():
    """A read-only Alpaca client. The 'daily' account's keys are fine for the
    shared market-data endpoints — news is not account-scoped."""
    from alpaca_rest import AlpacaClient
    return AlpacaClient("daily")


def fetch(client, symbols: list[str] | None = None, hours: int = 24,
          limit: int = 20) -> list[dict]:
    """Recent news items, newest first: [{ts, headline, summary, symbols, source}].

    One request; symbol filtering is done server-side via the comma list so the
    items returned are relevant to the watchlist rather than the whole tape.
    """
    key = f"{','.join(sorted(symbols or []))}|{hours}|{limit}"
    cached = CACHE.get(key)
    if cached is not None:
        return cached

    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = [f"start={start}", f"limit={int(limit)}", "sort=desc"]
    if symbols:
        params.append("symbols=" + ",".join(sym.strip().upper() for sym in symbols if sym))
    try:
        data = client._request("GET", f"/v1beta1/news?{'&'.join(params)}",
                               base=client.data_base_url)
    except Exception:
        return CACHE.put(key, [])       # fail open: no news, not an error

    items: list[dict] = []
    for n in (data or {}).get("news", []) or []:
        items.append({
            "ts": n.get("created_at"),
            "headline": (n.get("headline") or "").strip(),
            "summary": (n.get("summary") or "").strip(),
            "symbols": n.get("symbols") or [],
            "source": n.get("source"),
        })
    return CACHE.put(key, items)


def brief(client=None, symbols: list[str] | None = None, hours: int = 24,
          limit: int = 12, max_chars: int = 1400) -> str:
    """A compact digest for an LLM prompt: '- META: <headline> — <summary>'.

    Empty string when there is nothing (a missing news feed must read as
    "nothing to add", never as a fabricated headline).
    """
    try:
        items = fetch(client or _client(), symbols=symbols, hours=hours, limit=limit)
    except Exception:
        return ""
    if not items:
        return ""
    lines: list[str] = []
    for it in items:
        tags = ",".join((it["symbols"] or [])[:4])
        head = it["headline"][:_MAX_HEADLINE]
        summ = it["summary"][:_MAX_SUMMARY]
        when = (it["ts"] or "")[11:16]
        prefix = f"{tags}: " if tags else ""
        lines.append(f"- [{when}Z] {prefix}{head}" + (f" — {summ}" if summ else ""))
        if sum(len(x) for x in lines) > max_chars:
            lines.append("- …")
            break
    return "\n".join(lines)


def symbol_news(symbol: str, client=None, hours: int = 72, limit: int = 5,
                max_chars: int = 600) -> str:
    """Headlines for ONE symbol — used to sanity-check a single-name entry
    (YOLO bought 1 META on 2026-09-17 while holding overnight; a stop-loss does
    not protect an earnings or headline gap)."""
    try:
        items = fetch(client or _client(), symbols=[symbol], hours=hours, limit=limit)
    except Exception:
        return ""
    out: list[str] = []
    for it in items:
        if symbol.upper() not in [s.upper() for s in (it["symbols"] or [])]:
            continue
        out.append(f"- {(it['ts'] or '')[:10]} {it['headline'][:_MAX_HEADLINE]}")
        if sum(len(x) for x in out) > max_chars:
            break
    return "\n".join(out)


def earnings_mentions(symbol: str, client=None, hours: int = 240) -> list[str]:
    """Headlines mentioning earnings/results for `symbol` — a cheap backstop for
    the case where the structured earnings calendar has no coverage (ETFs, small
    caps). Returns the matching headlines."""
    try:
        items = fetch(client or _client(), symbols=[symbol], hours=hours, limit=25)
    except Exception:
        return []
    keys = ("earnings", "results", "quarter", "q1", "q2", "q3", "q4", "guidance")
    hits = []
    for it in items:
        text = f"{it['headline']} {it['summary']}".lower()
        if any(k in text for k in keys):
            hits.append(it["headline"])
    return hits[:4]
