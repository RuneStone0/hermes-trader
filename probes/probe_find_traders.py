#!/usr/bin/env python3
"""Find public X accounts that post their own trades WITHOUT a paywall.

Two passes, because the first one cannot be trusted on its own:

  PASS A — discovery. An open x_search (NOT restricted to one handle) asking for
  accounts that publish their own entries/exits with tickers and no paid tier.
  The model is required to ground every claim in posts it actually read through
  the tool, and to hand back the post ids so they can be checked.

  PASS B — verification. For each candidate, a SEPARATE search restricted to that
  handle (allowed_x_handles), asking what they have actually posted lately and
  whether their recent timeline pushes a paid service. This is the pass that
  catches a plausible-sounding handle with no trade posts behind it, or one whose
  "free" calls are funnelled into a Discord/Substack.

Output: a JSON dossier on stdout (and to /tmp/traders.json). Nothing here writes
to the trading database.

Usage: python3 probes/probe_find_traders.py [max_candidates]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                    # noqa: E402
from xfeed import FeedError                                      # noqa: E402

DISCOVERY_SYS = (
    "You search X for accounts that publicly post their OWN trades. "
    "Rules you must not break:\n"
    "- Only report an account if you actually read posts from it through the "
    "search tool in this conversation.\n"
    "- Quote real post ids/URLs. Never invent a handle, a post id or a URL. If "
    "you are unsure an account exists, leave it out.\n"
    "- 'Posts trades' means: states entries and/or exits with a ticker. Market "
    "commentary, price predictions and 'watchlist' posts do not count.\n"
    "- 'Paywalled' means a paid subscription is needed to see the actual calls: "
    "a Discord/Patreon/Substack/paid group, 'link in bio', 'DM me', or the trade "
    "being shown only to subscribers. Free accounts that merely link a free "
    "newsletter are NOT paywalled.\n"
    "- Track record: report only what they themselves demonstrate (a public "
    "statement, a linked verified track record page, dated screenshots). Say "
    "exactly what the evidence is, and say plainly when there is none.\n"
    "Be skeptical: an account with a big following is not evidence of a good "
    "record.\n"
    "Respond with ONLY a JSON object:\n"
    '{"candidates":[{"handle":"","name":"","posts_entries_and_exits":true,'
    '"paywalled":false,"paywall_detail":"","followers_estimate":"",'
    '"market_focus":"","posting_frequency":"","track_record_claim":"",'
    '"evidence_posts":[{"id":"","url":"","text":""}],"why_include":""}],'
    '"search_notes":""}'
)

VERIFY_SYS = (
    "You are auditing ONE X account for a trading bot that may follow it. Read "
    "its recent posts with the search tool and answer from what you SAW, not "
    "from what you know about the account. Never invent a post, id or URL.\n"
    "Answer these, one field each:\n"
    "- recent_trade_posts: the most recent posts where they state a position or "
    "an exit with a TICKER, with real ids/urls and the verbatim text.\n"
    "- paywall_signals: any promotion of a paid service (Discord, paid group, "
    "Substack paid tier, 'link in bio', 'DM me') in the recent posts, quoted.\n"
    "- posts_entries_and_exits: true only if BOTH an entry and an exit are "
    "visible with tickers in what you read.\n"
    "- shows_stops_or_targets: do their posts state stop levels or targets?\n"
    "- verifiable_track_record: is there any linked, checkable record of "
    "results (a track-record page, broker statement, public P/L log)? Describe "
    "it or say none seen.\n"
    "- looks_like_pump_or_spam: promotional, vague or hype-driven rather than "
    "a trader posting their own book?\n"
    "- confidence: 0.0-1.0 that this account really posts its own trades "
    "publicly and free.\n"
    "- summary: two sentences a non-expert can read.\n"
    "Respond with ONLY a JSON object:\n"
    '{"handle":"","recent_trade_posts":[{"id":"","url":"","date":"","text":""}],'
    '"paywall_signals":[],"posts_entries_and_exits":false,'
    '"shows_stops_or_targets":false,"verifiable_track_record":"",'
    '"looks_like_pump_or_spam":false,"confidence":0.0,"summary":""}'
)


def ask(prompt: str, system: str, handles: list[str] | None = None) -> dict:
    """One grounded x_search call. Raises if the tool was never used."""
    if not config.XAI_API_KEY:
        raise FeedError("XAI_API_KEY is not configured")
    tool: dict = {"type": "x_search"}
    if handles:
        tool["allowed_x_handles"] = handles
    body = {
        "model": config.X_FEED["xai_model"],
        "input": [{"role": "system", "content": system},
                  {"role": "user", "content": prompt}],
        "tools": [tool],
        "tool_choice": "required",
    }
    req = urllib.request.Request(
        f"{config.X_FEED['xai_base_url'].rstrip('/')}/responses",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {config.XAI_API_KEY}",
                 "Content-Type": "application/json",
                 "User-Agent": "hermes-trader/probe"},
        method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode())
    used = ((d.get("usage") or {}).get("server_side_tool_usage_details") or {})
    if not any(str(k).startswith("x_") and v for k, v in used.items()):
        raise FeedError("no tool call behind this answer — treated as a guess")
    text = ""
    for item in (d.get("output") or []):
        if item.get("type") == "message":
            for c in (item.get("content") or []):
                if c.get("type") == "output_text":
                    text += c.get("text") or ""
    if not text:
        raise FeedError("empty answer")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        s = s.split("\n", 1)[1] if "/" in s.split("\n", 1)[0] else s
    return json.loads(s)


def nnum(v, default: float = 0.0) -> float:
    """A number out of whatever the model returned (it sometimes quotes them)."""
    try:
        return float(str(v).strip().rstrip("%")) if v is not None else default
    except (TypeError, ValueError):
        return default


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    # An optional custom discovery question: the same harness is reused to ask a
    # different question of the same corpus (live entries vs watchlists vs
    # checkable records), because each question surfaces a different population.
    custom = sys.argv[2] if len(sys.argv) > 2 else None

    print("PASS A — discovery (open search, not restricted to a handle)", flush=True)
    found = ask(custom or (
        "Search X for accounts that post their own stock trades publicly and "
        "for free: they state entries and exits with tickers, show their "
        "position sizing or stops, and do NOT require a paid subscription to "
        "see the calls. Include a mix: some well known, some smaller. For each "
        "one, find at least one real post of theirs that states a ticker and an "
        "action, and give me its id and url. Also report any account that claims "
        f"a strong track record with visible results rather than just claims. "
        f"Return up to {limit} accounts, best first."), DISCOVERY_SYS)
    cands = [c for c in (found.get("candidates") or []) if c.get("handle")]
    print(f"  {len(cands)} candidate(s): {[c.get('handle') for c in cands]}", flush=True)
    print(f"  notes: {str(found.get('search_notes'))[:300]}", flush=True)

    print("\nPASS B — verification, one restricted search per handle", flush=True)
    dossier = []
    for c in cands[:limit]:
        h = str(c["handle"]).lstrip("@").strip()
        try:
            v = ask(f"Audit @{h}. Read their recent posts and answer the fields.",
                    VERIFY_SYS, handles=[h])
        except (FeedError, json.JSONDecodeError, OSError) as e:
            v = {"error": str(e)[:200]}
        v["handle"] = h
        v["discovery"] = c
        v["posts_seen_in_verification"] = len(v.get("recent_trade_posts") or [])
        dossier.append(v)
        conf = nnum(v.get("confidence"))
        flag = "OK " if conf >= 0.6 else "?? "
        print(f"  {flag}@{h}: conf={conf} "
              f"entries_and_exits={v.get('posts_entries_and_exits')} "
              f"paywall={v.get('paywall_signals') or 'none'} "
              f"posts={v['posts_seen_in_verification']}", flush=True)
        print(f"        {str(v.get('summary'))[:220]}", flush=True)

    out = {"discovery_notes": found.get("search_notes"), "candidates": dossier}
    # Timestamped per pass: several passes over the same corpus ask DIFFERENT
    # questions and return different populations, and the first version of this
    # overwrote one pass's dossier with the next one's.
    import time as _t
    path = Path(f"/tmp/traders_{_t.strftime('%Y%m%d_%H%M%S', _t.gmtime())}.json")
    path.write_text(json.dumps(out, indent=1))
    Path("/tmp/traders_latest.json").write_text(json.dumps(out, indent=1))
    print(f"\nwrote {path} ({len(dossier)} audited accounts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
