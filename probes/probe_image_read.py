#!/usr/bin/env python3
"""Can the feed read a position that is only shown in the attached IMAGE?

Why this matters: 25 of the 31 posts the live bot has stored carry media, and the
two posts where he actually hinted at a trade ("can't wait to blast all of this
at my next full port play") were parked as 'review' precisely because the signal
was only in the picture. If the image can be read, those stop being dead ends.

This asks the SAME narrow, grounded question the feed already asks, with the
image made the subject: what does the attached image in post X show, and does it
state a position? It prints the answer AND whether the search tool was actually
used (an answer with no tool call is a guess, and is reported as one).

Usage: python3 probes/probe_image_read.py [post_id] [handle]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                    # noqa: E402

# One of the two posts the live bot parked as image-only (week of 2026-09-29).
DEFAULT_POST = "2104582454606799186"
DEFAULT_HANDLE = "fullportnik"

SYS = (
    "You answer questions about ONE X post using the search tool. Report only "
    "what the post and its attached media actually show. If an image is present, "
    "say what a reader can SEE in it (a broker position, a chart, a P&L screen, "
    "a ticker, a price, a stop) and quote any text visible in the image. If you "
    "cannot see the image, say exactly that — do not guess what a trading "
    "screenshot would probably contain.\n"
    "Respond with ONLY a JSON object:\n"
    '{"image_present":false,"image_readable":false,"what_is_visible":"",'
    '"position_stated":false,"symbol":"","side":"","entry":null,"stop":null,'
    '"quote_from_image":"","confidence":0.0}'
)


def main() -> int:
    pid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_POST
    handle = (sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HANDLE).lstrip("@")
    if not config.XAI_API_KEY:
        print("XAI_API_KEY is not configured")
        return 1

    prompt = (f"Post id {pid} by @{handle}. Look it up with the search tool. "
              f"Does it have an attached image, and if so what does that image "
              f"show? Does the image state any position (ticker, side, entry, "
              f"stop)? Quote any text you can read in the image.")
    body = {
        "model": config.X_FEED["xai_model"],
        "input": [{"role": "system", "content": SYS},
                  {"role": "user", "content": prompt}],
        "tools": [{"type": "x_search", "allowed_x_handles": [handle]}],
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
    tool_used = any(str(k).startswith("x_") and v for k, v in used.items())
    text = ""
    for item in (d.get("output") or []):
        if item.get("type") == "message":
            for c in (item.get("content") or []):
                if c.get("type") == "output_text":
                    text += c.get("text") or ""

    print(f"post   : {pid} (@{handle})")
    print(f"tool   : {'called' if tool_used else 'NOT CALLED — answer is a guess'}")
    print(f"usage  : {json.dumps(used)}")
    print(f"raw    : {text.strip()[:1200]}")
    try:
        print("\nparsed : " + json.dumps(json.loads(text.strip()), indent=1))
    except (json.JSONDecodeError, ValueError):
        print("\nparsed : unreadable as JSON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
