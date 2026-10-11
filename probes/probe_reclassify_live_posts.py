#!/usr/bin/env python3
"""Do the new mention rules fire on this account's REAL posts?

Re-reads every post the live bot has stored with the CURRENT classifier and
prints what it now concludes, next to what it concluded at the time. This is the
only honest way to answer "will following his named interests actually catch
anything?" — 12 days of his real output is the evidence, not a synthetic post.

Usage: python3 probes/probe_reclassify_live_posts.py [/tmp/live_posts.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import x_copy_run as X                                            # noqa: E402


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/live_posts.json")
    posts = json.loads(path.read_text())
    posts.sort(key=lambda p: str(p.get("posted_at") or ""), reverse=True)

    would_trade, changed, failed = [], [], 0
    for p in posts:
        try:
            a = X.analyse(p)
        except Exception as e:                                     # noqa: BLE001
            failed += 1
            print(f"  !! {str(p.get('posted_at'))[:16]}  analysis failed: {e}")
            continue
        state, reason = X._disposition(p, a)
        flag = "TRADE" if state == "open" else "     "
        was = str(p.get("kind") or "?")
        mark = " <-- CHANGED" if was != a["kind"] else ""
        print(f"  {flag} {str(p.get('posted_at'))[:16]} {was:<8}->{a['kind']:<8} "
              f"sym={','.join(a['symbols']) or '-':<12} conf={a['confidence']:.2f} "
              f"{state}{mark}")
        print(f"        {a['reason'][:150]}")
        if state == "open":
            would_trade.append((p, a, reason))
        if was != a["kind"]:
            changed.append(p)

    print(f"\n{len(posts)} posts re-read, {failed} failed to classify")
    print(f"would now open a position: {len(would_trade)}")
    for p, a, reason in would_trade:
        print(f"  - {str(p.get('posted_at'))[:16]} {a['kind']} {a['symbols']}: {reason[:110]}")
    print(f"kind changed vs the stored read: {len(changed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
