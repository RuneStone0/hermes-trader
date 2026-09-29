#!/usr/bin/env python3
"""Test the reliability of NARROW questions against the xAI X-search tool:
  Q1: what is the newest original post's id?  (the trigger)
  Q2: verbatim text of a specific post id.    (the detail fetch)
Narrow facts should be far more repeatable than a wide enumeration."""
import json, pathlib, sys, urllib.request, urllib.error, time

env = {}
for line in pathlib.Path("/opt/data/.env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
KEY = env["XAI_API_KEY"]
HANDLE = "fullportnik"


def ask(user, schema, tools=None, extra_note=""):
    body = {
        "model": "grok-4-fast",
        "input": [{"role": "system", "content": extra_note},
                  {"role": "user", "content": user}],
        "tools": tools or [{"type": "x_search", "allowed_x_handles": [HANDLE]}],
        "tool_choice": "required",
        "text": {"format": {"type": "json_schema", "name": "out", "schema": schema,
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
    u = (d.get("usage") or {}).get("server_side_tool_usage_details") or {}
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out[:300], "_tools": u}


NEWEST_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "created_utc": {"type": "string"},
        "text": {"type": "string"},
        "is_reply": {"type": "boolean"},
    },
    "required": ["id", "created_utc", "text", "is_reply"], "additionalProperties": False,
}
DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"}, "url": {"type": "string"},
        "created_utc": {"type": "string"}, "text": {"type": "string"},
        "is_reply": {"type": "boolean"}, "has_media": {"type": "boolean"},
    },
    "required": ["id", "url", "created_utc", "text", "is_reply", "has_media"],
    "additionalProperties": False,
}

print("=== Q1: newest ORIGINAL post (5 runs) ===")
ids = []
for i in range(5):
    r = ask(
        f"Using the x_search tool (keyword query 'from:{HANDLE} -filter:replies' in "
        f"Latest mode), what is the SINGLE MOST RECENT original (non-reply) post by "
        f"@{HANDLE}? Return its exact numeric post id, its exact UTC timestamp, its "
        f"verbatim text, and whether it is a reply.",
        NEWEST_SCHEMA,
        extra_note="You are a fact extractor. You MUST call the x_search tool. Report only "
                   "what the tool returned. Never invent a post id.")
    ids.append(r.get("id"))
    print(f"  run {i+1}: id={r.get('id')} at={r.get('created_utc')} reply={r.get('is_reply')} "
          f"text={str(r.get('text'))[:60]!r}{r.get('_error') or ''}")
    time.sleep(1)
print("distinct newest ids:", sorted(set(ids)))

KNOWN = "2104582454606799186"
print(f"\n=== Q2: verbatim text of post {KNOWN} (3 runs) ===")
for i in range(3):
    r = ask(f"Using the x_search tool, give me the verbatim text, exact URL and exact UTC "
            f"timestamp of the X post with id {KNOWN} (https://x.com/{HANDLE}/status/{KNOWN}).",
            DETAIL_SCHEMA,
            extra_note="You are a fact extractor. You MUST call the x_search tool.")
    print(f"  run {i+1}: id={r.get('id')} at={r.get('created_utc')} "
          f"text={str(r.get('text'))[:90]!r}{r.get('_error') or ''}")
    time.sleep(1)
