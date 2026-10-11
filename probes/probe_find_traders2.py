#!/usr/bin/env python3
"""Second pass: accounts worth following, scored on the two axes that matter.

Pass 1 (probe_find_traders.py) asked who posts trades for free and came back
with a lot of promotional accounts, because that is what the keyword surface is
full of. This pass asks the harder question directly: WHO can be checked, and
WHO posts an entry while it is still news?

  AXIS 1 — can the record be checked? A third-party verified track record
  (Kinfo / Myfxbook / Collective2 / Fundseeder / a public audited log), or
  dated public posts that can be scored after the fact. "I made $467k" is not
  evidence.

  AXIS 2 — is the signal usable for free, in time? An entry post naming the
  ticker while the trade is live, ideally with a stop, and no paid tier standing
  between the reader and the call.

Output: JSON on stdout + /tmp/traders2.json. Read-only; touches no trading data.

Usage: python3 probes/probe_find_traders2.py [max_candidates]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                    # noqa: E402
# The grounded-search helper and number coercion live in the first probe; this
# pass only changes the QUESTIONS, so it reuses them rather than forking them.
from probe_find_traders import ask, nnum                         # noqa: E402

DISCOVERY_SYS = (
    "You search X for accounts a trading bot could plausibly follow. You must "
    "ground EVERY claim in posts you actually read through the search tool in "
    "this conversation, and give real post ids/urls. Never invent a handle, a "
    "post or a url; leave an account out if you cannot point at a post.\n"
    "Two things make an account worth reporting, and you must be explicit about "
    "which ones it satisfies:\n"
    "A) CHECKABLE RECORD: a track record that someone else can verify — a "
    "third-party verified profile (Kinfo, Myfxbook, Collective2, Fundseeder, "
    "Brokerstatement, an audited P/L page), or a long run of dated public posts "
    "that can be scored after the fact. A screenshot of a profit is NOT "
    "checkable. Say exactly what the evidence is.\n"
    "B) USABLE FREE SIGNAL: they post the trade while it is live — ticker plus "
    "entry, ideally a stop — and the call is visible WITHOUT paying (no paid "
    "Discord/Substack/group, no 'link in bio' for the actual call). Promoting a "
    "free newsletter is fine and must NOT be counted as a paywall; requiring "
    "payment to see the call must be.\n"
    "Prioritise accounts that satisfy BOTH. Report single-A or single-B accounts "
    "only if they are unusually strong, and label them.\n"
    "Be skeptical and say so: large following is not evidence, and an account "
    "that only posts results after the fact is not a signal source.\n"
    "Respond with ONLY a JSON object:\n"
    '{"candidates":[{"handle":"","axis_checkable":"","axis_free_signal":"",'
    '"both_axes":false,"what_they_post":"","posting_cadence":"",'
    '"verification_links":[],"evidence_posts":[{"id":"","url":"","text":""}],'
    '"verdict":""}],"search_notes":""}'
)

VERIFY_SYS = (
    "You are auditing ONE X account for a bot that may follow it. Read its "
    "recent posts with the search tool and answer ONLY from what you saw. Never "
    "invent a post, id or url.\n"
    "- live_entries: posts that named a ticker AT THE TIME of entry (not a "
    "retrospective), with real ids/urls and verbatim text.\n"
    "- exits_and_results: do they post exits or results, and are the results "
    "dated so they could be scored?\n"
    "- checkable_record: any third-party verifiable record (Kinfo/Myfxbook/"
    "Collective2/Fundseeder/audited page) with the link, or none seen.\n"
    "- paywall_evidence: quote the post that demands payment for the call, or "
    "say none.\n"
    "- free_newsletter_only: do they merely promote a free newsletter/link?\n"
    "- post_cadence: how often do they actually post trades?\n"
    "- would_a_bot_survive: what would happen to a bot copying this account — "
    "one sentence.\n"
    "- confidence: 0.0-1.0 that this is a real, free, checkable source.\n"
    "- summary: two sentences for a non-expert.\n"
    "Respond with ONLY a JSON object:\n"
    '{"handle":"","live_entries":[{"id":"","url":"","date":"","text":""}],'
    '"exits_and_results":"","checkable_record":"","paywall_evidence":"",'
    '"free_newsletter_only":false,"post_cadence":"","would_a_bot_survive":"",'
    '"confidence":0.0,"summary":""}'
)


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    print("PASS A — discovery: checkable record AND/OR usable free signal", flush=True)
    found = ask(
        "Find X accounts that post their own trades publicly for free, and/or "
        "have a track record that a third party can verify. I want accounts "
        "where the entry is posted while the trade is live with a ticker (a stop "
        "is a plus), and where the results could be checked rather than taken on "
        "trust. Include swing traders, day traders and options sellers, well "
        "known and small, but EXCLUDE any account whose calls require a paid "
        f"subscription or group. Return up to {limit} accounts, best first.",
        DISCOVERY_SYS)
    cands = [c for c in (found.get("candidates") or []) if c.get("handle")]
    print(f"  {len(cands)} candidate(s): {[c.get('handle') for c in cands]}", flush=True)
    print(f"  notes: {str(found.get('search_notes'))[:400]}", flush=True)

    print("\nPASS B — verification per handle", flush=True)
    dossier = []
    for c in cands[:limit]:
        h = str(c["handle"]).lstrip("@").strip()
        try:
            v = ask(f"Audit @{h}. Read their recent posts and answer the fields.",
                    VERIFY_SYS, handles=[h])
        except Exception as e:                                     # noqa: BLE001
            v = {"error": str(e)[:200]}
        v["handle"] = h
        v["discovery"] = c
        dossier.append(v)
        conf = nnum(v.get("confidence"))
        print(f"  {'OK ' if conf >= 0.6 else '?? '}@{h}: conf={conf} "
              f"live_entries={len(v.get('live_entries') or [])} "
              f"record={str(v.get('checkable_record'))[:70]!r} "
              f"paywall={str(v.get('paywall_evidence'))[:60]!r}", flush=True)
        print(f"        {str(v.get('summary'))[:230]}", flush=True)

    import time as _t
    path = Path(f"/tmp/traders2_{_t.strftime('%Y%m%d_%H%M%S', _t.gmtime())}.json")
    path.write_text(json.dumps(
        {"discovery_notes": found.get("search_notes"), "candidates": dossier}, indent=1))
    print(f"\nwrote {path} ({len(dossier)} audited accounts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
