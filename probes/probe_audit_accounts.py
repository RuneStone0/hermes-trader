#!/usr/bin/env python3
"""Audit named X accounts against the two axes.

Pass 2 came back with no account satisfying BOTH "checkable record" and "usable
free signal" and named two near-misses: one with live entries and no record, one
with a verified record and no live entries. This audits a list of handles with
the same verifier so near-misses can be looked at directly rather than through a
discovery query that has to guess what is interesting.

Usage: python3 probes/probe_audit_accounts.py handle [handle ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from probe_find_traders import ask, nnum                         # noqa: E402
from probe_find_traders2 import VERIFY_SYS                        # noqa: E402


def main() -> int:
    handles = [h.lstrip("@").strip() for h in sys.argv[1:] if h.strip()]
    if not handles:
        print(__doc__)
        return 1
    out = []
    for h in handles:
        try:
            v = ask(f"Audit @{h}. Read their recent posts and answer the fields.",
                    VERIFY_SYS, handles=[h])
        except Exception as e:                                     # noqa: BLE001
            v = {"handle": h, "error": str(e)[:200]}
        v["handle"] = h
        out.append(v)
        print("=" * 78)
        print(f"@{h}  conf={nnum(v.get('confidence'))}")
        print(f"  live entries ({len(v.get('live_entries') or [])}):")
        for e in (v.get("live_entries") or [])[:5]:
            print(f"    • {str(e.get('date'))[:12]} {str(e.get('url'))[:58]}")
            print(f"      {str(e.get('text'))[:200]}")
        for k in ("exits_and_results", "checkable_record", "paywall_evidence",
                  "free_newsletter_only", "post_cadence", "would_a_bot_survive",
                  "summary"):
            print(f"  {k}: {str(v.get(k))[:260]}")
    Path("/tmp/traders_audit.json").write_text(json.dumps(out, indent=1))
    print(f"\nwrote /tmp/traders_audit.json ({len(out)} accounts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
