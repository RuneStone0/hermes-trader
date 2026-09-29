"""X post feed for the copy-trade follower (x_copy_run.py).

WHY IT IS BUILT THIS WAY
------------------------
The bot's whole job hangs off one question asked every few minutes: "has he
posted anything new?" If the feed answers that wrongly the failure is silent —
no error, no alert, just a missed trade — so the feed is designed around the
reliability of the X index, measured rather than assumed.

Measured on 2026-09-29 against a real handle with 6 posts in a 4-day window:

  * "list the last N posts" (one wide enumeration) returned 2, 3, 5 and 6 of the
    same 6 posts on four consecutive runs. Unusable as a trigger: it silently
    loses posts, and it loses them randomly.
  * "what is the most recent original post?" was correct on 5 of 5 runs.
  * "which post came immediately before post <id>?" was correct 3 of 3.
  * "give me the verbatim text of post <id>" was correct 3 of 3.

So the feed never enumerates. It asks narrow, verifiable questions and walks the
timeline one post at a time:

    newest -> previous -> previous -> ... -> a post we already have

A normal poll (nothing new) costs ONE call. A poll that finds two new posts
costs three. The walk is bounded, and it stops the moment it reaches a post we
already hold, so it is cheap exactly when the account is quiet.

Two modes:
  trigger  walk back until we reach a post we already have (bounded by
           `max_walk`). Runs every poll; this is the trade trigger.
  sweep    walk back a fixed `sweep_depth` posts regardless of what we already
           have. Runs every `sweep_interval_h` hours as insurance against a post
           that reached the search index late (an index that lags is the one
           known weakness of this source), and against the bot having been down.

When the official X API credentials are configured (`X_BEARER_TOKEN`) the feed
uses it instead: the timeline endpoint is exact and complete, which makes the
walk unnecessary. That is the upgrade path, and it needs no other change here.

A read that fails NEVER returns an empty list — it raises FeedError. "We could
not check" and "he posted nothing" must never look the same.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config

X_API = "https://api.x.com/2"
_RETRYABLE = (429, 500, 502, 503, 504)


class FeedError(RuntimeError):
    """The feed could not be read. Never means 'no new posts'."""


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value) -> str | None:
    """Best-effort ISO-8601 UTC ('2026-09-28T14:42:13Z') from a provider value."""
    if isinstance(value, (int, float)):
        return _iso(datetime.fromtimestamp(float(value), tz=timezone.utc))
    s = str(value or "").strip()
    if not s:
        return None
    s = s.replace(" UTC", "Z")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%b %d, %Y %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _iso(dt)


def _post(handle: str, pid, url: str | None, created: str | None, text: str | None,
          is_reply: bool, has_media: bool, source: str) -> dict | None:
    """Validate + normalise one raw provider item, or None if unusable.

    A post without a numeric id is dropped: the id is the only thing that makes
    a post dedupeable, and the id is also the anchor every follow-up question
    uses. A fabricated or malformed id would poison the whole walk.
    """
    pid = str(pid or "").strip()
    if not pid.isdigit():
        return None
    handle = handle.lstrip("@")
    ts = _parse_time(created)
    if not ts:
        return None
    body = str(text or "").strip()
    if not body and not has_media:
        return None
    return {
        "id": pid,
        "author": handle,
        "url": str(url or "").strip() or f"https://x.com/{handle}/status/{pid}",
        # Named `posted_at` to match the x_posts column it is stored in: the two
        # dicts (feed post, database row) are used interchangeably downstream, so
        # a different name here is exactly how a KeyError gets shipped.
        "posted_at": ts,
        "text": body,
        "is_reply": bool(is_reply),
        "has_media": bool(has_media),
        "source": source,
    }


def _dedupe(posts: list[dict]) -> list[dict]:
    """Newest first, one entry per id (post ids increase with time)."""
    seen: dict[str, dict] = {}
    for p in posts:
        if p and p["id"] not in seen:
            seen[p["id"]] = p
    return sorted(seen.values(), key=lambda p: (p["posted_at"], int(p["id"])), reverse=True)


# --------------------------------------------------------------------------- #
# Provider: official X API (exact; used when a bearer token is configured)
# --------------------------------------------------------------------------- #
def _xapi_get(path: str, params: dict, token: str, timeout: int) -> dict:
    url = f"{X_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "hermes-trader/copy-feed",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise FeedError(f"X API HTTP {e.code}: {e.read().decode()[:200]}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise FeedError(f"X API unreachable: {e}") from None


def _xapi_user_id(handle: str, token: str, timeout: int) -> str:
    d = _xapi_get(f"/users/by/username/{handle}", {"user.fields": "id"}, token, timeout)
    uid = str((d.get("data") or {}).get("id") or "")
    if not uid:
        raise FeedError(f"X API could not resolve @{handle}: {str(d)[:160]}")
    return uid


def _xapi_posts(handle: str, since_iso: str | None, max_posts: int,
                timeout: int) -> list[dict]:
    token = config.X_BEARER_TOKEN
    uid = _xapi_user_id(handle, token, timeout)
    out: list[dict] = []
    token_next = None
    for _ in range(3):                                   # 3 pages x 100 = enough
        params = {
            "max_results": min(100, max(5, max_posts - len(out))),
            "exclude": "replies,retweets",
            "tweet.fields": "created_at,attachments,referenced_tweets",
        }
        if since_iso:
            params["start_time"] = since_iso
        if token_next:
            params["pagination_token"] = token_next
        d = _xapi_get(f"/users/{uid}/tweets", params, token, timeout)
        for t in d.get("data") or []:
            refs = t.get("referenced_tweets") or []
            p = _post(handle, t.get("id"), f"https://x.com/{handle}/status/{t.get('id')}",
                      t.get("created_at"), t.get("text"),
                      bool(t.get("in_reply_to_user_id")) or any(
                          r.get("type") == "replied_to" for r in refs),
                      bool((t.get("attachments") or {}).get("media_ids")),
                      "x_api")
            if p:
                out.append(p)
        token_next = (d.get("meta") or {}).get("next_token")
        if not token_next or len(out) >= max_posts:
            break
    return out


# --------------------------------------------------------------------------- #
# Provider: xAI Responses API with the server-side x_search tool
# --------------------------------------------------------------------------- #
_XAI_SYS = (
    "You are a fact extractor working over the X (Twitter) search tool. You MUST "
    "call the x_search tool before answering, and you must report ONLY what the "
    "tool returned. Never invent a post, an id, a timestamp or a quote — a made-up "
    "id is worse than an empty answer. If the tool does not show you the post you "
    "were asked about, reply with found=false and empty fields."
)

_POST_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "id": {"type": "string"},
        "created_utc": {"type": "string"},
        "text": {"type": "string"},
        "is_reply": {"type": "boolean"},
        "has_media": {"type": "boolean"},
    },
    "required": ["found", "id", "created_utc", "text", "is_reply", "has_media"],
    "additionalProperties": False,
}


class XaiClient:
    """Thin wrapper over POST https://api.x.ai/v1/responses (x_search tool)."""

    def __init__(self, timeout: int | None = None, model: str | None = None):
        self.timeout = int(timeout or config.X_FEED["timeout_s"])
        self.model = model or config.X_FEED["xai_model"]
        self.calls = 0

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{config.X_FEED['xai_base_url'].rstrip('/')}/responses",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {config.XAI_API_KEY}",
                     "Content-Type": "application/json",
                     "User-Agent": "hermes-trader/copy-feed"},
            method="POST")
        last = None
        for attempt in range(3):
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                code = e.code
                last = f"HTTP {code}: {e.read().decode()[:200]}"
                if code not in _RETRYABLE:
                    break
            except (urllib.error.URLError, TimeoutError) as e:
                last = f"network: {e}"
            time.sleep(1.5 * (attempt + 1))
        raise FeedError(f"xAI request failed — {last}")

    def ask_post(self, prompt: str, handle: str) -> dict | None:
        """One narrow question about a single post. Returns a raw dict or None.

        Raises FeedError when the tool was never called: an answer with no tool
        call behind it is a model guess, which is exactly the thing that would
        fabricate a post id.
        """
        if not config.XAI_API_KEY:
            raise FeedError("XAI_API_KEY is not configured")
        body = {
            "model": self.model,
            "input": [{"role": "system", "content": _XAI_SYS},
                      {"role": "user", "content": prompt}],
            "tools": [{"type": "x_search", "allowed_x_handles": [handle]}],
            "tool_choice": "required",
            "text": {"format": {"type": "json_schema", "name": "post",
                                "schema": _POST_SCHEMA, "strict": True}},
        }
        d = self._post(body)
        used = ((d.get("usage") or {}).get("server_side_tool_usage_details") or {})
        if not any(str(k).startswith("x_") and v for k, v in used.items()):
            raise FeedError("the model answered without calling the search tool")
        text = self._output_text(d)
        if not text:
            raise FeedError("empty answer from the model")
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            raise FeedError(f"unreadable answer: {text[:160]}") from None
        if not isinstance(out, dict):
            raise FeedError(f"unexpected answer shape: {text[:160]}")
        return out

    @staticmethod
    def _output_text(d: dict) -> str:
        out = ""
        for o in d.get("output") or []:
            if o.get("type") == "message":
                for c in o.get("content") or []:
                    if c.get("type") == "output_text":
                        out += c.get("text") or ""
        return out.strip()


def _newest(handle: str, cli: XaiClient) -> dict | None:
    """The most recent ORIGINAL post. The one question the bot depends on."""
    r = cli.ask_post(
        f"Using the x_search tool with the keyword query "
        f"'from:{handle} -filter:replies' in Latest mode: what is the SINGLE MOST "
        f"RECENT original (non-reply) post by @{handle}? Give its exact numeric post "
        f"id, its exact UTC timestamp (ISO-8601, e.g. 2026-09-28T14:42:13Z), its "
        f"verbatim text, whether it is a reply, and whether it has attached media. "
        f"If the tool shows no posts at all, set found=false.", handle)
    if not r or not r.get("found"):
        return None
    return _post(handle, r.get("id"), None, r.get("created_utc"), r.get("text"),
                 bool(r.get("is_reply")), bool(r.get("has_media")), "xai")


def _previous(handle: str, cursor: dict, cli: XaiClient) -> dict | None:
    """The original post published immediately BEFORE `cursor`."""
    day = str(cursor["posted_at"])[:10]
    r = cli.ask_post(
        f"Using the x_search tool with the keyword query "
        f"'from:{handle} -filter:replies until:{day}' in Latest mode: which ORIGINAL "
        f"(non-reply) post by @{handle} was published IMMEDIATELY BEFORE the post "
        f"with id {cursor['id']} (published {cursor['posted_at']})? Give that "
        f"earlier post's id, exact UTC timestamp, verbatim text, whether it is a "
        f"reply, and whether it has media. If the tool shows no earlier post, set "
        f"found=false.", handle)
    if not r or not r.get("found"):
        return None
    p = _post(handle, r.get("id"), None, r.get("created_utc"), r.get("text"),
              bool(r.get("is_reply")), bool(r.get("has_media")), "xai")
    if p and p["id"] == str(cursor["id"]):
        return None                          # a repeat is not a step backwards
    return p


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def fetch_posts(handle: str | None = None, known_ids: set[str] | None = None,
                mode: str = "trigger", since_iso: str | None = None) -> dict:
    """Posts by `handle`, newest first.

    mode='trigger' walks back only as far as the first post in `known_ids`.
    mode='sweep'   walks back `sweep_depth` posts whatever we already have.

    Returns {'provider', 'posts', 'error', 'attempts', 'calls', 'gap_suspect'}
    where posts excludes replies when config.X_FEED['include_replies'] is False.
    """
    feed = config.X_FEED
    handle = (handle or feed["handle"]).lstrip("@")
    known_ids = {str(i) for i in (known_ids or set())}
    attempts: list[dict] = []
    out: dict = {"provider": None, "posts": [], "error": None, "attempts": attempts,
                 "calls": 0, "gap_suspect": False}

    # --- official API first: exact and complete, so no walk is needed -------- #
    if config.X_BEARER_TOKEN:
        try:
            posts = _xapi_posts(handle, since_iso, int(feed["max_posts"]),
                                int(feed["timeout_s"]))
            attempts.append({"provider": "x_api", "ok": True,
                             "detail": f"{len(posts)} post(s)"})
            out.update(provider="x_api", posts=posts)
            out["calls"] = 1
            return _finish(out, feed)
        except FeedError as e:
            attempts.append({"provider": "x_api", "ok": False, "detail": str(e)})
            # Falls through to xAI rather than failing: a second opinion is
            # better than a blind spot, and the attempt list records both.

    # --- xAI: narrow questions, walking the timeline ----------------------- #
    cli = XaiClient()
    try:
        newest = _newest(handle, cli)
    except FeedError as e:
        attempts.append({"provider": "xai", "ok": False, "detail": str(e)})
        out["error"] = str(e)
        out["calls"] = cli.calls
        return out

    if newest is None:
        # The tool answered, called the tool, and saw nothing: a genuinely empty
        # account, which is not an error.
        attempts.append({"provider": "xai", "ok": True, "detail": "no posts visible"})
        out.update(provider="xai")
        out["calls"] = cli.calls
        return _finish(out, feed)

    posts = [newest]
    if newest["id"] in known_ids and mode == "trigger":
        # Already current: one call, nothing to do. The common case.
        attempts.append({"provider": "xai", "ok": True, "detail": "up to date"})
        out.update(provider="xai", posts=_finish_posts(posts, feed))
        out["calls"] = cli.calls
        return out

    depth = int(feed["sweep_depth"] if mode == "sweep" else feed["max_walk"])
    cutoff = _iso(datetime.now(timezone.utc)
                  - timedelta(days=int(feed.get("lookback_days", 30)) + 1))
    cursor = newest
    stopped_at_known = cursor["id"] in known_ids
    ran_out = False
    try:
        for step in range(max(0, depth)):
            # Two independent bounds on the walk: the depth cap above, and the
            # lookback window here. Posts older than the window are discarded
            # anyway, so walking past them would only spend money.
            if str(cursor["posted_at"]) < cutoff:
                break
            prev = _previous(handle, cursor, cli)
            if prev is None:
                # The tool says there is nothing earlier. On a first run (nothing
                # held yet) that is simply the edge of the account; with posts
                # already held it means we never reached our own timeline, which
                # is a gap worth reporting.
                ran_out = True
                break
            posts.append(prev)
            cursor = prev
            if prev["id"] in known_ids:
                stopped_at_known = True
                if mode == "trigger":
                    break
            if any(p["id"] == prev["id"] for p in posts[:-1]):
                break                                     # defensive: no loop
    except FeedError as e:
        # The walk broke part-way. Whatever we already collected is real, so keep
        # it, but flag the gap: the next sweep will pick up anything missed.
        attempts.append({"provider": "xai", "ok": False, "detail": f"walk: {e}"})
        out["gap_suspect"] = True

    if ran_out and not stopped_at_known and known_ids:
        out["gap_suspect"] = True

    attempts.append({"provider": "xai", "ok": True,
                     "detail": f"walked {cli.calls} call(s), {len(posts)} post(s)"})
    out.update(provider="xai", posts=_finish_posts(posts, feed))
    out["calls"] = cli.calls
    return out


def _finish(out: dict, feed: dict) -> dict:
    out["posts"] = _finish_posts(out["posts"], feed)
    return out


def _finish_posts(posts: list[dict], feed: dict) -> list[dict]:
    """Drop replies (unless asked for) and anything outside the lookback."""
    if not feed.get("include_replies", False):
        posts = [p for p in posts if not p["is_reply"]]
    cutoff = _iso(datetime.now(timezone.utc)
                  - timedelta(days=int(feed.get("lookback_days", 30))))
    posts = [p for p in posts if p["posted_at"] >= cutoff]
    return _dedupe(posts)


def provider_status() -> dict:
    """What the feed would use right now — for /health and the dashboard."""
    return {
        "handle": config.X_FEED["handle"],
        "providers_configured": [p for p in config.X_FEED["providers"]
                                 if (p != "xai" or config.XAI_API_KEY)
                                 and (p != "x_api" or config.X_BEARER_TOKEN)],
        "xai_configured": bool(config.XAI_API_KEY),
        "x_api_configured": bool(config.X_BEARER_TOKEN),
        "model": config.X_FEED["xai_model"] if config.XAI_API_KEY else None,
        "poll_interval_s": config.X_FEED["poll_interval_s"],
        "include_replies": bool(config.X_FEED["include_replies"]),
    }


if __name__ == "__main__":                                   # manual smoke test
    import db
    db.init_db()
    res = fetch_posts(known_ids={str(r["post_id"]) for r in db.x_posts(limit=500)})
    print(json.dumps({k: v for k, v in res.items() if k != "posts"}, indent=2))
    for p in res["posts"]:
        print(f"\n{p['id']}  {p['posted_at']}  reply={p['is_reply']} media={p['has_media']}")
        print(f"  {p['url']}")
        print(f"  {' '.join((p['text'] or '').split())[:200]}")
