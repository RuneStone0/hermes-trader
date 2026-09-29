#!/usr/bin/env python3
"""Measure the xAI feed's RECALL: run the same fetch N times and compare which
post ids came back. A trade copier that silently drops a post drops a trade, so
this is the property that matters most."""
import collections, sys, time
sys.path.insert(0, "/opt/data/profiles/trader/trading")
import xfeed

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
seen = collections.Counter()
first = {}
runs = []
for i in range(RUNS):
    r = xfeed.fetch_posts()
    ids = [p["id"] for p in r["posts"]]
    runs.append(ids)
    for p in r["posts"]:
        seen[p["id"]] += 1
        first.setdefault(p["id"], p["created_at"])
    print(f"run {i+1}: provider={r['provider']} n={len(ids)} err={r['error']}")
    time.sleep(2)

print("\n--- id frequency across runs ---")
for pid, n in sorted(seen.items(), key=lambda kv: str(kv[0]), reverse=True):
    print(f"{n}/{RUNS}  {pid}  {first[pid]}")
print("\nunique ids:", len(seen))
