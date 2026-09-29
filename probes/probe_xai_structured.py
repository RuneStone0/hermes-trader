#!/usr/bin/env python3
"""Probe: can we get STRUCTURED, verbatim posts from an X handle out of the
xAI Responses API (x_search server-side tool) instead of a prose summary?"""
import json, os, pathlib, urllib.request, urllib.error

env = {}
for line in pathlib.Path("/opt/data/.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
KEY = env["XAI_API_KEY"]

SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "the numeric post id from the x.com/status/<id> URL"},
                    "url": {"type": "string"},
                    "created_utc": {"type": "string", "description": "ISO8601 UTC timestamp"},
                    "text": {"type": "string", "description": "verbatim post text"},
                    "is_reply": {"type": "boolean"},
                    "has_media": {"type": "boolean"},
                },
                "required": ["id", "url", "created_utc", "text", "is_reply", "has_media"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["posts"],
    "additionalProperties": False,
}

body = {
    "model": "grok-4-fast",
    "input": [
        {"role": "system", "content":
            "You are a raw X post extractor. You MUST call the x_search tool to look up the "
            "requested posts (use a keyword query of the form 'from:fullportnik -filter:replies "
            "since:<date>' in Latest mode). Report ONLY posts you actually retrieved from the tool, "
            "verbatim. Never invent a post. If the tool returns nothing new, return an empty list."},
        {"role": "user", "content":
            "Return every ORIGINAL (non-reply) post by @fullportnik from 2026-09-26 onwards, newest first. "
            "Give the exact numeric post id, the exact x.com URL, the UTC timestamp, the verbatim text, "
            "whether it was a reply, and whether it had media attached."},
    ],
    "tools": [{"type": "x_search", "allowed_x_handles": ["fullportnik"]}],
    "tool_choice": "required",
    "text": {"format": {"type": "json_schema", "name": "posts", "schema": SCHEMA, "strict": True}},
}

req = urllib.request.Request(
    "https://api.x.ai/v1/responses",
    data=json.dumps(body).encode(),
    headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode())
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:2000]); raise SystemExit(1)

print("model:", d.get("model"), "status:", d.get("status"))
print("usage:", json.dumps(d.get("usage")))
for o in d.get("output") or []:
    t = o.get("type")
    if t == "custom_tool_call":
        print(f"[tool_call] {o.get('name')} input={str(o.get('input'))[:300]}")
    elif t == "message":
        for c in o.get("content") or []:
            if c.get("type") == "output_text":
                print("[output_text]", c.get("text")[:3000])
    elif t == "reasoning":
        print("[reasoning summary]", (o.get("summary") or [{}])[0].get("text", "")[:200] if o.get("summary") else "")
