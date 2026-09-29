#!/usr/bin/env python3
"""Is 'the post immediately BEFORE post X' a reliable narrow question?

The wide 'list the last N posts' query returned 2-6 of 6 items across runs, so it
cannot be trusted to catch a gap. If the backward step is reliable we can walk the
timeline one post at a time instead of enumerating it."""
import json, pathlib, time, urllib.error, urllib.request

env = {}
for line in pathlib.Path("/opt/data/.env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
KEY = env["XAI_API_KEY"]
HANDLE = "fullportnik"
NEWEST = "2104582454606799186"      # known newest original post
EXPECT = "2104270654493122840"      # the one before it (2026-09-27T18:03:14Z)

SYS = ("You are a fact extractor. You MUST call the x_search tool and report only "
       "what it returned. Never invent a post id. If the tool does not show you the "
       "post, say so with found=false.")
SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "id": {"type": "string"},
        "created_utc": {"type": "string"},
        "text": {"type": "string"},
        "is_reply": {"type": "boolean"},
    },
    "required": ["found", "id", "created_utc", "text", "is_reply"],
    "additionalProperties": False,
}


def ask(user):
    body = {
        "model": "grok-4-fast",
        "input": [{"role": "system", "content": SYS}, {"role": "user", "content": user}],
        "tools": [{"type": "x_search", "allowed_x_handles": [HANDLE]}],
        "tool_choice": "required",
        "text": {"format": {"type": "json_schema", "name": "out", "schema": SCHEMA,
                            "strict": True}},
    }
    req = urllib.request.Request(
        "https://api.x.ai/v1/responses", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    out = ""
    for o in d.get("output") or []:
        if o.get("type") == "message":
            for c in o.get("content") or []:
                if c.get("type") == "output_text":
                    out += c.get("text") or ""
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out[:200]}


Q = (f"Using the x_search tool with the keyword query "
     f"'from:{HANDLE} -filter:replies until:2026-09-28' in Latest mode: which ORIGINAL "
     f"(non-reply) post by @{HANDLE} was published IMMEDIATELY BEFORE the post with id "
     f"{NEWEST} (published 2026-09-28T14:42:13Z)? Give that previous post's id, exact "
     f"UTC timestamp, verbatim text and whether it is a reply.")

print(f"=== walking back from {NEWEST} (expect {EXPECT}) ===")
hits = 0
for i in range(4):
    r = ask(Q)
    ok = str(r.get("id")) == EXPECT
    hits += ok
    print(f"  run {i+1}: found={r.get('found')} id={r.get('id')} "
          f"at={r.get('created_utc')} {'MATCH' if ok else 'MISS'}"
          f"{r.get('_error') or r.get('_raw') or ''}")
    print(f"         text={str(r.get('text'))[:70]!r}")
    time.sleep(1)
print(f"correct: {hits}/4")
