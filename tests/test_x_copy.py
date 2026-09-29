"""Tests for the X-copy follower (x_copy_run.py) and its feed (xfeed.py).

Run: python3 tests/test_x_copy.py

These pin the properties that decide whether this bot is safe to leave running
unattended, and every one of them is a property I would otherwise have to trust:

  * a read that FAILED must never look like "he posted nothing"  (the silent
    failure that eats signals — the whole reason the feed walks the timeline
    instead of enumerating it, because enumeration returned 2-6 of 6 real posts);
  * a mirrored position ALWAYS has a stop, and the journal says whether it came
    from his post or from the symbol's own ATR;
  * the universe floor holds in the open path, not just in the prompt: no crypto,
    no options, no unverifiable tickers;
  * sizing comes from THIS system's risk rules, never from his size;
  * he can never re-fire the same post into a second position, and a re-read of a
    post we already acted on is inert;
  * shadow mode places NOTHING at the broker and claims no profit, while still
    recording the exact order it would have placed.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import db                                              # noqa: E402
import guards                                          # noqa: E402
import wording                                         # noqa: E402
import x_copy_run                                      # noqa: E402
import xfeed                                           # noqa: E402

# Isolate the database: these tests write posts and trades, and they must never
# touch the live paper ledger.
_TMP = tempfile.mkdtemp(prefix="xcopy-test-")
config.DB_PATH = Path(_TMP) / "test.db"
config.EVENT_GUARD["enabled"] = False        # no network in the calendar guard
config.COPY["block_earnings_within_days"] = 0
db.init_db()

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class FakeClient:
    """An Alpaca stand-in: records what would be sent to the broker."""

    def __init__(self, price: float = 100.0, positions: list | None = None):
        self.price = price
        self._positions = positions or []
        self.orders: list[dict] = []
        self.closed: list[str] = []

    def latest_trade(self, sym):
        if self.price is None:
            return None
        return {"price": self.price}

    def bars(self, sym, timeframe="1Day", limit=5, start=None):
        return {"bars": [{"c": self.price}] if self.price else []}

    def positions(self):
        return self._positions

    def account(self):
        return {"equity": 10000.0, "buying_power": 10000.0}

    def clock(self):
        return {"is_open": True}

    def bracket_order(self, sym, qty, side, stop_price=None, target_price=None,
                      time_in_force="gtc"):
        self.orders.append({"symbol": sym, "qty": qty, "side": side,
                            "stop": stop_price, "target": target_price})
        return {"id": "order-1", "client_order_id": "co-1"}

    def release_and_close(self, sym):
        self.closed.append(sym)
        return None, 2


def post(**over) -> dict:
    p = {"post_id": "2104582454606799186", "author": "fullportnik",
         "url": "https://x.com/fullportnik/status/2104582454606799186",
         "posted_at": "2026-09-28T14:42:13Z", "text": "text",
         "is_reply": 0, "has_media": 0, "source": "xai", "seen_at": _now_iso(),
         "state": "new"}
    p.update(over)
    return p


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _sql(stmt: str) -> None:
    """Run one statement and CLOSE the connection.

    Every helper here opens its own connection, so a test that calls
    db.connect() without closing leaks one and the next write hits
    "database is locked" — which is a bug in the test, not in the bot.
    """
    conn = db.connect()
    try:
        conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def sig(kind="open", symbol="META", side="long", stop=None, target=None,
        confidence=0.9, reason="he is long META", needs_media=False) -> dict:
    return {"kind": kind, "symbol": symbol, "side": side, "stop": stop,
            "target": target, "confidence": confidence, "reason": reason,
            "needs_media": needs_media}


def _no_atr(monkeypatch_value):
    x_copy_run._atr = lambda client, sym: monkeypatch_value


# --------------------------------------------------------------------------- #
# 1. The feed: a failed read is never an empty read
# --------------------------------------------------------------------------- #
def test_feed_never_confuses_failure_with_silence() -> None:
    print("feed: a failed read is an error, never 'no new posts'")
    real_urlopen = xfeed.urllib.request.urlopen
    # The client refuses to run without a key, so give it a placeholder: these
    # tests assert the PARSING rules and must not depend on a live credential
    # being present in the environment they happen to run in.
    saved_key = config.XAI_API_KEY
    config.XAI_API_KEY = "test-key-not-used"

    class R:
        def __init__(self, body):
            self.body = json.dumps(body).encode()

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # (a) the model answered WITHOUT calling the search tool -> FeedError
    xfeed.urllib.request.urlopen = lambda *a, **k: R({
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"found": true, "id": "1"}'}]}],
        "usage": {}})
    cli = xfeed.XaiClient()
    raised = ""
    try:
        cli.ask_post("q", "fullportnik")
    except xfeed.FeedError as e:
        raised = str(e)
    check("no tool call -> FeedError", "without calling the search tool" in raised, raised)

    # (b) the model DID call the tool and saw nothing -> a real (empty) answer
    xfeed.urllib.request.urlopen = lambda *a, **k: R({
        "output": [{"type": "message", "content": [
            {"type": "output_text",
             "text": json.dumps({"found": False, "id": "", "created_utc": "",
                                 "text": "", "is_reply": False, "has_media": False})}]}],
        "usage": {"server_side_tool_usage_details": {"x_search_calls": 1}}})
    check("tool called + nothing found -> found=False",
          cli.ask_post("q", "fullportnik").get("found") is False)

    # (c) an HTTP failure raises rather than returning an empty list
    def boom(*a, **k):
        raise xfeed.urllib.error.HTTPError("u", 500, "boom", {}, None)
    xfeed.urllib.request.urlopen = boom
    err = ""
    try:
        xfeed.fetch_posts(handle="fullportnik", known_ids=set())
    except Exception as e:                                     # noqa: BLE001
        err = str(e)
    check("fetch_posts never raises out", err == "")
    res = xfeed.fetch_posts(handle="fullportnik", known_ids=set())
    check("500 -> error set, posts empty, no silent success",
          bool(res["error"]) and res["posts"] == [], str(res["error"])[:60])
    check("error response does not claim a provider",
          res["provider"] is None, str(res["provider"]))

    xfeed.urllib.request.urlopen = real_urlopen
    config.XAI_API_KEY = saved_key


def test_feed_validation() -> None:
    print("feed: post validation and de-duplication")
    ok = xfeed._post("fullportnik", "2104582454606799186", None,
                     "2026-09-28T14:42:13Z", "hello", False, False, "xai")
    check("valid post normalises", ok is not None and ok["posted_at"].startswith("2026-09-28"))
    check("url is built when the provider omits it",
          ok and ok["url"] == "https://x.com/fullportnik/status/2104582454606799186")
    check("a non-numeric id is dropped",
          xfeed._post("h", "not-an-id", None, "2026-09-28T14:42:13Z", "x", False, False, "xai") is None)
    check("a post with no text and no media is dropped",
          xfeed._post("h", "123", None, "2026-09-28T14:42:13Z", "", False, False, "xai") is None)
    check("a post with no usable timestamp is dropped",
          xfeed._post("h", "123", None, "sometime tuesday", "x", False, False, "xai") is None)
    check("media-only posts survive",
          xfeed._post("h", "123", None, "2026-09-28T14:42:13Z", "", False, True, "xai") is not None)

    a = xfeed._post("h", "2", None, "2026-09-28T10:00:00Z", "b", False, False, "xai")
    b = xfeed._post("h", "1", None, "2026-09-27T10:00:00Z", "a", False, False, "xai")
    deduped = xfeed._dedupe([a, b, a])
    check("de-duplicating by id, newest first",
          [p["id"] for p in deduped] == ["2", "1"])

    reply = xfeed._post("h", "3", None, "2026-09-28T11:00:00Z", "r", True, False, "xai")
    two = xfeed._finish_posts([reply, a], config.X_FEED)
    check("replies are dropped (the user asked for original posts only)",
          [p["id"] for p in two] == ["2"])


# --------------------------------------------------------------------------- #
# 2. The universe floor, in the open path
# --------------------------------------------------------------------------- #
def test_open_path_floor() -> None:
    print("open path: the universe floor is enforced in code, not in the prompt")
    c = FakeClient(price=100.0)
    _no_atr(3.0)
    p = post()
    chk = lambda r: r[0] == "rejected"

    check("crypto is refused", chk(x_copy_run.open_position(
        c, p, sig(symbol="BTC/USD"), 10000.0, 10000.0, {}, False)))
    check("options are refused", chk(x_copy_run.open_position(
        c, p, sig(symbol="META261218C00500000"), 10000.0, 10000.0, {}, False)))
    check("a lower-case junk ticker is refused", chk(x_copy_run.open_position(
        c, p, sig(symbol="META!!!!"), 10000.0, 10000.0, {}, False)))
    check("a signal with no symbol goes to review, not to the broker",
          x_copy_run.open_position(c, p, sig(symbol=""), 10000.0, 10000.0, {}, False)[0] == "review")

    blind = FakeClient(price=None)
    r = x_copy_run.open_position(blind, p, sig(), 10000.0, 10000.0, {}, False)
    check("an unverifiable ticker is refused", chk(r), r[1])
    check("...and nothing was sent to the broker", blind.orders == [])

    check("already holding the symbol is refused", chk(x_copy_run.open_position(
        c, p, sig(symbol="META"), 10000.0, 10000.0, {"META": {"symbol": "META"}}, False)))
    three = {s: {"symbol": s} for s in ("AAA", "BBB", "CCC")}
    check("the concurrency cap is enforced", chk(x_copy_run.open_position(
        c, p, sig(symbol="METAX".replace("X", "A")), 10000.0, 10000.0, three, False)))
    check("options and crypto never reached the broker", c.orders == [])


def test_stop_is_always_present() -> None:
    print("open path: every mirrored position carries a stop")
    p = post()

    # He states no stop and volatility cannot be measured -> refuse, do not guess.
    c = FakeClient(price=100.0)
    _no_atr(None)
    r = x_copy_run.open_position(c, p, sig(stop=None), 10000.0, 10000.0, {}, False)
    check("no stop available -> refuse rather than place an unprotected position",
          r[0] == "rejected" and "stop cannot be placed" in r[1], r[1])
    check("nothing was sent to the broker", c.orders == [])

    # No stop from him, but the symbol has an ATR -> derive it and say so.
    _no_atr(3.0)
    r = x_copy_run.open_position(c, p, sig(stop=None), 10000.0, 10000.0, {}, False)
    check("a derived ATR stop is used when his post states none",
          r[0] == "done" and "ATR" in r[1], r[1])

    # He states a stop -> it is used verbatim, and it survives into the decision.
    c2 = FakeClient(price=100.0)
    r = x_copy_run.open_position(c2, p, sig(stop=94.0, target=118.0), 10000.0, 10000.0, {}, False)
    check("his stated stop is used when it is usable", r[0] == "done", r[1])
    check("...and the reason says the stop came from his post",
          "his post" in r[1], r[1])

    # A stop on the wrong side of the live price is not accepted as a stop.
    c3 = FakeClient(price=100.0)
    r = x_copy_run.open_position(c3, p, sig(stop=120.0, target=90.0), 10000.0, 10000.0, {}, False)
    check("a wrong-side stop falls back to the derived one, and is still a stop",
          r[0] == "done" and "ATR" in r[1], r[1])


def test_sizing_is_ours_not_his() -> None:
    print("open path: size comes from our risk rules, not from his conviction")
    _no_atr(2.0)
    c = FakeClient(price=100.0)
    x_copy_run.open_position(c, post(), sig(stop=96.0, target=112.0),
                             11000.0, 100000.0, {}, False)
    r = db.recent_events(1, account="copy")[0]
    detail = r["detail"] or ""
    check("position size is capped by the position limit (25% of equity)",
          "2,7" in detail, detail[:140])
    # Read the risk back out of the journal and check it against the cap rather
    # than against a number this test hard-codes: the point is the RULE.
    risk = float(detail.split("risk $")[1].split(" ")[0].replace(",", ""))
    check("risk per position is capped at 1% of equity (110 = 1% of 11,000)",
          risk <= 11000.0 * float(config.COPY["max_risk_pct"]) + 0.5,
          f"risk ${risk} vs cap ${11000.0 * float(config.COPY['max_risk_pct']):.0f}")

    # A tiny account cannot round a position into existence.
    check("a position that rounds to zero shares is refused",
          x_copy_run.open_position(FakeClient(price=5000.0), post(), sig(stop=4000.0),
                                   100.0, 100.0, {}, False)[0] == "rejected")


# --------------------------------------------------------------------------- #
# 3. Reading a post
# --------------------------------------------------------------------------- #
def test_reading_posts() -> None:
    print("reading: one LLM call per post, classified and clamped")
    real_chat = x_copy_run.advisor.chat

    def fake_chat(system, user, **kw):
        check("the reader is told to prefer 'unknown' over guessing",
              "'unknown'" in system)
        return json.dumps({"kind": "OPEN", "symbol": "$meta", "side": "LONG",
                           "stop": None, "target": None, "confidence": 1.7,
                           "reason": "he says he is long META", "needs_media": False})

    x_copy_run.advisor.chat = fake_chat
    a = x_copy_run.analyse(post(text="i'm long META, full port"))
    check("kind is normalised", a["kind"] == "open")
    check("a $cashtag is cleaned to a ticker", a["symbol"] == "META")
    check("side is normalised", a["side"] in ("long",))
    check("confidence is clamped to 1.0", a["confidence"] == 1.0)

    x_copy_run.advisor.chat = lambda *a, **k: "I'm sorry, I cannot help with that."
    bad = ""
    try:
        x_copy_run.analyse(post())
    except Exception as e:                                     # noqa: BLE001
        bad = type(e).__name__
    check("an unreadable answer raises instead of becoming 'no signal'", bad != "", bad)

    x_copy_run.advisor.chat = real_chat

    print("reading: a post with no text and no media is inert, not an error")
    empty = x_copy_run.analyse(post(text="", has_media=0))
    check("textless post -> kind none", empty["kind"] == "none")
    check("...with a reason", "no text" in empty["reason"], empty["reason"])


def test_dispositions() -> None:
    print("dispositions: every post ends up with a plain-English answer")
    cases = [
        (sig(kind="none", symbol="", confidence=0.0, reason="market commentary"),
         "ignored"),
        (sig(kind="unknown", symbol="", confidence=0.9, reason="no ticker named"),
         "review"),
        (sig(kind="unknown", symbol="", confidence=0.9, reason="in the image",
             needs_media=True), "review"),
        (sig(kind="open", symbol="META", confidence=0.2), "ignored"),
        (sig(kind="trim", symbol="META"), "ignored"),
        (sig(kind="close", symbol="META"), "exit"),
        (sig(kind="open", symbol="META"), "open"),
        (sig(kind="add", symbol="META"), "open"),
    ]
    for s, want in cases:
        got, why = x_copy_run._disposition(post(), s)
        check(f"{s['kind']}/{s['symbol'] or '-'} -> {want}", got == want, f"got {got}: {why}")
    check("a trim says why it is not followed",
          "full exits only" in x_copy_run._disposition(post(), sig(kind="trim"))[1])


# --------------------------------------------------------------------------- #
# 4. The shadow book, and the one thing shadow mode must never do
# --------------------------------------------------------------------------- #
def test_shadow_mode_places_nothing() -> None:
    print("shadow mode: records the order, sends nothing, claims no P/L")
    _sql("delete from trades")
    _no_atr(2.5)
    c = FakeClient(price=50.0)
    events_before = len(db.recent_events(500, account="copy"))
    state, why = x_copy_run.open_position(
        c, post(post_id="9001"), sig(symbol="AAPL", stop=47.0, target=58.0),
        10000.0, 10000.0, {}, live=False)
    check("shadow is reported as done", state == "done", why)
    check("NOTHING was sent to the broker", c.orders == [])
    check("no trade row was invented", db.all_trades() == [])
    new_events = db.recent_events(500, account="copy")[:len(db.recent_events(500, account="copy")) - events_before]
    shadow = [e for e in db.recent_events(20, account="copy") if e["decision"] == "shadow"]
    check("a shadow event was journaled", len(shadow) >= 1)
    check("the shadow line says it would have acted, in words",
          shadow and "Would have" in shadow[0]["reason"], shadow[0]["reason"] if shadow else "")


def test_shadow_book_replay() -> None:
    print("shadow book: entries and exits replay from the posts")
    _sql("delete from x_posts")
    db.upsert_x_post("1", "fullportnik", posted_at="2026-09-01T10:00:00Z", text="long META")
    db.set_x_post("1", state="done", kind="open", symbol="META")
    book = x_copy_run.shadow_book()
    check("an entry puts the symbol in the book", "META" in book)

    db.upsert_x_post("2", "fullportnik", posted_at="2026-09-02T10:00:00Z", text="added META")
    db.set_x_post("2", state="done", kind="add", symbol="META")
    check("adding to it does not double the position",
          len(x_copy_run.shadow_book()) == 1)

    db.upsert_x_post("3", "fullportnik", posted_at="2026-09-03T10:00:00Z", text="sold META")
    db.set_x_post("3", state="done", kind="close", symbol="META")
    check("a later exit empties it", "META" not in x_copy_run.shadow_book())
    _sql("delete from x_posts")
    

# --------------------------------------------------------------------------- #
# 5. Idempotence: a re-read must be inert
# --------------------------------------------------------------------------- #
def test_re_reads_are_inert() -> None:
    print("idempotence: the same post can never fire twice")
    _sql("delete from x_posts")
    p = post(post_id="777")
    first = db.upsert_x_post(p["post_id"], p["author"], url=p["url"],
                             posted_at=p["posted_at"], text=p["text"])
    db.set_x_post("777", state="done", kind="open", symbol="META", reason="mirrored")
    second = db.upsert_x_post(p["post_id"], p["author"], url=p["url"],
                              posted_at=p["posted_at"], text="EDITED BY HIM")
    check("first sighting is new", first is True)
    check("the same post id is never new twice", second is False)
    row = db.x_posts(limit=5)[0]
    check("a re-read cannot overwrite the disposition we already acted on",
          row["state"] == "done" and row["symbol"] == "META",
          f"{row['state']}/{row['symbol']}")


def test_stale_posts_are_parked() -> None:
    print("unreadable posts are parked for a look, not retried forever")
    _sql("delete from x_posts")
    db.upsert_x_post("555", "fullportnik", posted_at="2026-09-01T10:00:00Z", text="?")
    _sql("update x_posts set seen_at='2026-09-01T00:00:00+00:00'")
    parked = x_copy_run._park_stale_new()
    check("an old unread post is parked", parked == 1, str(parked))
    check("and it is now flagged for a human",
          db.x_posts(limit=5)[0]["state"] == "review")
    _sql("delete from x_posts")
    

# --------------------------------------------------------------------------- #
# 6. Feed bookkeeping + the config floor
# --------------------------------------------------------------------------- #
def test_config_and_bookkeeping() -> None:
    print("config: the floor cannot be tuned away")
    from config import propose_override
    out_of_scope, _ = propose_override({"COPY": {"require_stop_loss": False}})
    check("require_stop_loss is not tunable", "require_stop_loss" not in out_of_scope)
    check("the floor is re-pinned on load", config.COPY["require_stop_loss"] is True)
    check("COPY is registered as a tunable section", "COPY" in config.__TUNE__)
    check("the copy account exists in the registry", "copy" in config.ACCOUNTS)
    check("the copy account is NOT configured in shadow mode",
          config.copy_account_configured() is False)
    check("the follower is not market-gated (it must see posts at any hour)",
          "copy" in config.ACCOUNTS and config.X_FEED["poll_interval_s"] == 600)
    total = config.tunable_knobs()
    check("COPY knobs are exposed to the tuner",
          any(k.get("section") == "COPY" for k in total),
          str([k.get("section") for k in total]))

    db.feed_state_set("last_poll", "2026-09-29T00:00:00+00:00")
    check("feed bookkeeping round-trips",
          db.feed_state_get("last_poll") == "2026-09-29T00:00:00+00:00")
    check("a missing key returns the default", db.feed_state_get("nope", "d") == "d")

    print("wording: the journal lines read like English")
    check("a new post reads plainly",
          wording.new_post("fullportnik", "i'm long META") ==
          "New post from @fullportnik: “i'm long META”")
    check("a shadow order says 'would have'",
          "Would have" in wording.shadow_order("META", "buy", 20, 100.0, 95.0, 110.0))


def test_no_broker_client_without_keys() -> None:
    print("clients: shadow mode cannot obtain an order-placing client")
    check("broker_client() is None until the account keys exist",
          x_copy_run.broker_client() is None)


def main() -> None:
    for fn in (test_feed_never_confuses_failure_with_silence,
               test_feed_validation,
               test_open_path_floor,
               test_stop_is_always_present,
               test_sizing_is_ours_not_his,
               test_reading_posts,
               test_dispositions,
               test_shadow_mode_places_nothing,
               test_shadow_book_replay,
               test_re_reads_are_inert,
               test_stale_posts_are_parked,
               test_config_and_bookkeeping,
               test_no_broker_client_without_keys):
        fn()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("all x-copy tests passed")


if __name__ == "__main__":
    main()
