"""X-copy follower — mirrors the positions a public X account posts.

WHAT IT DOES, IN ORDER
  1. POLL the account's ORIGINAL posts (replies are ignored by design: he replies
     constantly and those are conversation, not signals) through xfeed.py.
  2. STORE each post we have never seen, and journal it — so the dashboard is a
     live monitor of the account the user is following, independent of trading.
  3. READ each new post with one LLM call and classify it: an actionable entry,
     an exit, something that needs a human, or nothing at all. A post that merely
     hypes, polls, or brags about P&L is recorded as read-and-ignored with the
     reason, never quietly dropped.
  4. MIRROR the actionable ones: his SYMBOL and DIRECTION, sized by THIS system's
     risk rules — never his size. He goes all-in on one name; copying that at
     face value would put the whole book in a single overnight gap.

NON-NEGOTIABLE PROPERTIES
  * A feed that could not be read is NEVER reported as "no new posts". That is
    the failure that silently eats signals, so it is an error event every time.
  * Every mirrored position carries a stop-loss. If his post states one, it is
    used; if not, the stop is derived from the symbol's own ATR — and the journal
    says which of the two it was.
  * retail US equity/ETF only (no crypto but BTC, no options): enforced in the
    open path, not just in the prompt.
  * One position per symbol, one trade per post, never a re-fire on a re-read.
  * If the market is closed the ORDER waits for the open (a post is a signal, not
    a fill) and a signal older than COPY['max_signal_age_min'] is dropped rather
    than chased.

SHADOW MODE
  With no dedicated paper account configured (Alpaca caps a paper login at three,
  and all three are already in use), the bot runs the whole pipeline and journals
  the exact order it WOULD place — real symbol, real live price, real sizing and
  real levels — but submits nothing and invents no P/L. Filling in
  ALPACA_COPY_API_KEY / _SECRET_KEY turns it into real paper execution with no
  other change.

Usage: python3 x_copy_run.py [--dry-run] [--handle HANDLE]
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone

import advisor
import config
import db
import guards
import wording
import xfeed
from alpaca_rest import AlpacaClient, AlpacaError

ACCOUNT = "copy"
STRATEGY = "x_copy"

# ETFs (for the db asset_class label); anything else is an equity. ETFs never
# report earnings, which is why the earnings gate only ever bites single names.
ETF_SYMBOLS = {
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "IEF", "HYG",
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "SMH",
}

# A post left unread for this long is parked for review instead of being retried
# forever (the LLM failing twice in a row is a fault to surface, not a loop to run).
_NEW_POST_TIMEOUT_H = 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_min(iso: str) -> float | None:
    try:
        dt = datetime.fromisoformat(str(iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (_now() - dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def _hours_since(iso: str | None) -> float | None:
    mins = _age_min(iso) if iso else None
    return None if mins is None else mins / 60.0


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #
def data_client() -> AlpacaClient | None:
    """A read-only client for prices/clock (market data is account-independent).

    In shadow mode the copy account does not exist yet, so prices come from an
    existing account's key. Nothing here can place an order — every order path
    goes through broker_client().
    """
    for name in ("copy", "yolo", "daily", "weekly"):
        if name == "copy" and not config.copy_account_configured():
            continue
        if not config.ACCOUNTS.get(name, {}).get("api_key"):
            continue
        try:
            return AlpacaClient(name)
        except AlpacaError:
            continue
    return None


def broker_client() -> AlpacaClient | None:
    """The order-placing client, or None while running in shadow mode."""
    if not config.copy_account_configured():
        return None
    try:
        return AlpacaClient(ACCOUNT)
    except AlpacaError:
        return None


# --------------------------------------------------------------------------- #
# Reading a post
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You read ONE post from a public trader on X and decide, for a bot that "
    "follows his calls, whether it tells the bot anything it can act on. "
    "You are a strict extractor, not a commentator: report only what the post "
    "actually says. Never invent a ticker, and never treat his opinions about "
    "the market as a position.\n"
    "CONTEXT THAT MATTERS: his actual trade calls are largely behind a paywall. "
    "What he posts publicly is often a NAME he is interested in rather than a "
    "position he has taken. That kind of post is still useful to this bot, so it "
    "must be reported as 'interest' — do not throw it away as commentary, and do "
    "not inflate it into a position he never claimed.\n"
    "HOW TO CLASSIFY (kind):\n"
    "  'open'  — he states he HAS TAKEN or IS TAKING a position in a named "
    "instrument and names it (or names it beyond doubt, e.g. a $cashtag).\n"
    "  'add'   — he is adding to a named position he already said he holds.\n"
    "  'close' — he states he SOLD / exited / closed a SPECIFIC named position.\n"
    "  'trim'  — he states he sold PART of a named position.\n"
    "  'interest' — he names one or more SPECIFIC tradable instruments as things "
    "he likes, wants, is watching, is interested in or is considering, IN THE "
    "PRESENT OR FORWARD-LOOKING, and with a bullish or positive lean ('my "
    "favourite chart right now', 'names I like here', 'keeping an eye on X', "
    "'X looks good'), WITHOUT saying he has taken a position.\n"
    "  NOT 'interest' — be strict, these are 'none' even though a ticker is "
    "named:\n"
    "    * past-tense or past-performance commentary ('X has rallied', 'I sold X "
    "last month for a loss', 'X ran 30% since...');\n"
    "    * macro or correlation musings that merely cite a ticker ('yields up, "
    "and look at X');\n"
    "    * jokes, sarcasm or mockery about a ticker ('trading X is masochism'), "
    "or any mention he is bearish or dismissive about — he must be positive "
    "about the name for it to count;\n"
    "    * companies in the news, someone else's trade, comparisons, or a name "
    "used as an example rather than as something he wants.\n"
    "  'none'  — everything else: market commentary and macro talk with no "
    "instrument named, education, jokes, polls and engagement bait ('what should "
    "I full port next?'), P&L brag posts (a dollar figure with no instrument and "
    "no action), follower milestones, stream announcements, and statements that "
    "he is in cash / holding nothing (being flat is not a signal to close OUR "
    "positions). If a post names no specific tradable instrument, it is 'none'.\n"
    "  'unknown' — it LOOKS like a position but you cannot pin it down (he "
    "announces a trade without naming it; the position is only visible in an "
    "attached image; the ticker is ambiguous). Use this instead of guessing.\n"
    "FIELDS:\n"
    "  symbols — every SPECIFIC tradable ticker the post names that fits the "
    "classification above, best/most emphasised first, as ['META','AAPL']. An "
    "empty list when there is none. Real US-listed equity/ETF tickers only: no "
    "index symbols (SPX, NDX), no crypto pairs, no options, and never a company "
    "name you could not resolve beyond doubt. Do not pad the list.\n"
    "  symbol — the first entry of symbols (kept for the single-name readers).\n"
    "  side   — 'long' or 'short', '' when unclear. 'full port' means long. For "
    "'interest', use 'long' unless he clearly expresses a bearish view.\n"
    "  stop / target — ONLY a price he actually stated. Otherwise null. Do not "
    "invent levels; the bot derives its own and will say so.\n"
    "  confidence — 0.0-1.0: how sure you are that this post tells the bot about "
    "a specific instrument. Reserve >0.8 for an explicit stated position; a clear "
    "named interest can be 0.5-0.7; a passing or hedged mention belongs below "
    "0.4.\n"
    "  reason — one short plain-English sentence a non-expert can read.\n"
    "Respond with ONLY a JSON object (no markdown):\n"
    '{"kind":"open|add|close|trim|interest|none|unknown","symbols":[],"symbol":"",'
    '"side":"","stop":null,"target":null,"confidence":0.0,"reason":"",'
    '"needs_media":false}'
)


def analyse(post) -> dict:
    """One LLM read of one post. Raises AdvisorError if the answer is unreadable."""
    text = str(post["text"] or "").strip()
    if not text and not post["has_media"]:
        return {"kind": "none", "symbol": "", "symbols": [], "side": "",
                "stop": None, "target": None,
                "confidence": 0.0, "reason": "the post has no text and no media",
                "needs_media": False}
    payload = {
        "post": {
            "url": post["url"],
            "published_utc": post["posted_at"],
            "has_media": bool(post["has_media"]),
            "text": text or "(no text — media only)",
        },
        "follower_is_long_only_in_this_system": False,
        "note": ("If the only sign of a position is inside an attached image, set "
                 "kind='unknown' and needs_media=true — the text alone is what you "
                 "may act on."),
    }
    content = advisor.chat(_SYSTEM, json.dumps(payload, indent=2),
                           temperature=0.1, max_tokens=4000)
    out = advisor._extract_json(content)
    if not isinstance(out, dict) or not str(out.get("kind") or "").strip():
        raise advisor.AdvisorError(
            f"unreadable signal read ({len(content)} chars): {' '.join(content.split())[:160]}")
    return _clean_analysis(out)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def _ref_price(client: AlpacaClient, sym: str) -> float | None:
    """Live execution reference = the broker's LATEST TRADE (never a daily close:
    Alpaca validates bracket legs against the live price at submission)."""
    try:
        lt = client.latest_trade(sym)
        if lt and lt.get("price", 0) > 0:
            return float(lt["price"])
    except AlpacaError:
        pass
    try:
        bars = client.bars(sym, timeframe="1Day", limit=5, start=None).get("bars", [])
    except AlpacaError:
        return None
    return float(bars[-1]["c"]) if bars else None


def _atr(client: AlpacaClient, sym: str) -> float | None:
    """The symbol's own ATR(14) in dollars — the unit a derived stop is sized in."""
    try:
        import market_ctx
        snap = market_ctx.symbol_snapshot(client, sym) or {}
        atr = snap.get("atr14")
        return float(atr) if atr else None
    except Exception:                                              # noqa: BLE001
        return None


def _to_price(v, ref: float) -> float | None:
    """Accept an absolute price or a signed % move from ref ('-2.5%')."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, str) and v.strip().endswith("%"):
            return ref * (1 + float(v.strip()[:-1]) / 100.0)
        return float(v)
    except (TypeError, ValueError):
        return None


def _asset_class(sym: str) -> str:
    return "etf" if sym in ETF_SYMBOLS else "equity"


# A plain US ticker: 1-5 letters, optionally with a class suffix (BRK.B).
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")

# Quotes traders name that are NOT tradable on Alpaca retail: they pass the
# ticker regex, so they must be refused by name. SPY/QQQ/DIA/IWM are ETFs and are
# fine; these are the indices and volatility measures behind them.
_INDEX_SYMBOLS = {"SPX", "SPXW", "NDX", "VIX", "RUT", "DJI", "COMP", "XSP", "OEX",
                  "NYA", "SOX", "VIX9D"}


def _valid_symbol(sym: str) -> bool:
    """Retail US equity/ETF ticker shape. Options and crypto never match.

    Shape alone is not enough: SPX and NDX pass the regex and are not tradable
    on Alpaca retail (SPY is the proxy), so the index symbols are refused here
    as well as in the prompt — a model that names one must not be able to create
    a position in something the broker will reject.
    """
    s = str(sym or "").upper()
    return bool(_SYMBOL_RE.match(s)) and s not in _INDEX_SYMBOLS


def _clean_analysis(out: dict) -> dict:
    """Normalise one model answer into the shape the bot acts on.

    Kept separate from the model call so the parsing rules are testable without
    a live LLM: everything that decides what symbol/kind/side the bot sees from a
    post happens here.
    """
    kind = str(out.get("kind")).strip().lower()
    if kind not in ("open", "add", "close", "trim", "interest", "none", "unknown"):
        kind = "unknown"
    # `symbols` is the honest shape of what he posts (a "names I like" post can
    # list several); `symbol` is kept for every reader that handles one name.
    raw_syms = out.get("symbols")
    if isinstance(raw_syms, str):
        raw_syms = [raw_syms]
    if not isinstance(raw_syms, (list, tuple)):
        raw_syms = []
    if out.get("symbol"):
        raw_syms = list(raw_syms) + [out["symbol"]]
    symbols: list[str] = []
    for s in raw_syms:
        sym = "".join(ch for ch in str(s or "").upper() if ch.isalnum() or ch in "./")
        if sym and sym not in symbols and _valid_symbol(sym):
            symbols.append(sym)
    symbols = symbols[:5]
    symbol = symbols[0] if symbols else ""
    side = str(out.get("side") or "").strip().lower()
    if side not in ("long", "short"):
        side = ""
    if kind in ("open", "add", "interest") and not side:
        side = "long"          # his style is directional-long; the journal shows why
    try:
        confidence = max(0.0, min(1.0, float(out.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "kind": kind,
        "symbol": symbol,
        "symbols": symbols,
        "side": side,
        "stop": out.get("stop"),
        "target": out.get("target"),
        "confidence": confidence,
        "reason": str(out.get("reason") or "").strip() or "no reason given by the model",
        "needs_media": bool(out.get("needs_media")),
    }


def shadow_book() -> dict[str, dict]:
    """The symbols a SHADOW book would currently hold.

    Shadow mode writes no trade rows (inventing P/L for orders that were never
    placed would be a lie on the dashboard), so the book is replayed from the
    posts themselves: an entry marks a symbol held, a later exit releases it.
    Without this, 'I'm long META' followed by 'added to META' would journal two
    separate META positions instead of one.
    """
    rows = [r for r in db.x_posts(limit=500, states=("done",))]
    rows.sort(key=lambda r: (r["posted_at"] or "", str(r["post_id"])))
    book: dict[str, dict] = {}
    for r in rows:
        kind = str(r["kind"] or "")
        # A post can carry several names (a "names I like" post), and only some of
        # them may have been taken — `acted` is what the bot actually entered, so
        # the book reflects the positions and not the reading list.
        syms = [str(r["symbol"] or "").upper()]
        try:
            a = json.loads(r["analysis_json"] or "{}")
            syms = [str(s).upper() for s in (a.get("acted") or a.get("symbols") or syms)]
        except (json.JSONDecodeError, TypeError):
            pass
        syms = [s for s in syms if s]
        if kind in ("open", "add", "interest"):
            for s in syms:
                book[s] = dict(r)
        elif kind in ("close", "trim"):
            for s in syms:
                book.pop(s, None)
    return book


def _entry_symbols(row, a: dict) -> list[str]:
    """Which tickers this post asks the bot to act on.

    One, for a stated position. Several for a mention: a "names I like" post is
    a list, and taking only the first would throw away most of what he said. The
    cap keeps one post from filling the whole book.
    """
    kind = str(row["kind"] or "")
    if kind == "interest":
        syms = [str(s).strip().upper() for s in (a.get("symbols") or []) if s]
        if not syms and a.get("symbol"):
            syms = [str(a["symbol"]).strip().upper()]
        cap = int(config.COPY.get("mention_max_symbols", 2) or 1)
        return syms[:max(1, cap)]
    sym = str(a.get("symbol") or "").strip().upper()
    return [sym] if sym else []


def _entry_risk_mult(kind: str) -> float:
    """Mentions are sized down, not skipped: less information, same rules."""
    if str(kind) != "interest":
        return 1.0
    try:
        return max(0.05, min(1.0, float(config.COPY.get("mention_size_mult", 0.5))))
    except (TypeError, ValueError):
        return 0.5


def open_position(client: AlpacaClient, post, a: dict, equity: float,
                  buying_power: float, positions: dict, live: bool) -> tuple[str, str]:
    """Mirror an entry. Returns (state, journal_reason). Never raises.

    state is one of: 'done' | 'open' (queued for the open) | 'rejected' | 'review'
    """
    sym = str(a.get("symbol") or "").strip().upper()
    stop_src = "his post"

    # --- universe floor (retail US equity/ETF only) ------------------------- #
    if not sym:
        return "review", "he signalled a position without naming the instrument"
    if "/" in sym:
        return "rejected", (f"{sym}: crypto is outside this bot's universe "
                            "(the safety floor allows BTC only, which needs a separate feed)")
    if len(sym) >= 21:
        return "rejected", f"{sym}: options are outside this bot's universe"
    if not _valid_symbol(sym):
        return "rejected", f"{sym}: not a plain US equity/ETF ticker"

    if sym in positions:
        return "rejected", f"{sym}: already mirrored"
    if len(positions) >= int(config.COPY["max_concurrent_positions"]):
        return "rejected", (f"{sym}: already holding the maximum "
                            f"{config.COPY['max_concurrent_positions']} mirrored positions")

    # --- price the trade off the LIVE tape --------------------------------- #
    ref = _ref_price(client, sym)
    if ref is None or ref <= 0:
        return "rejected", f"{sym}: no live price — ticker could not be verified"

    long = a.get("side") != "short"
    min_stop = max(0.01, ref * float(config.COPY["min_stop_dist_pct"]))
    min_tgt = max(0.01, ref * float(config.COPY["min_target_dist_pct"]))

    # --- stop: his level if stated, otherwise derived from his symbol's ATR -- #
    stop = _to_price(a.get("stop"), ref)
    atr = None
    if stop is None or stop <= 0 or (long and stop >= ref) or ((not long) and stop <= ref):
        atr = _atr(client, sym)
        if not atr or atr <= 0:
            return "rejected", (f"{sym}: he stated no stop and the symbol's volatility "
                                "could not be measured, so a stop cannot be placed")
        stop = ref - float(config.COPY["atr_stop_mult"]) * atr if long \
            else ref + float(config.COPY["atr_stop_mult"]) * atr
        stop_src = f"derived from {sym}'s ATR ({atr:.2f})"
    if (long and stop > ref - min_stop) or ((not long) and stop < ref + min_stop):
        return "rejected", f"{sym}: stop {stop:.2f} sits inside the noise vs live {ref:.2f}"

    risk_dist = abs(ref - stop)
    if risk_dist <= 0:
        return "rejected", f"{sym}: zero risk distance"

    # --- target: his level if stated, else a multiple of the risk ----------- #
    target = _to_price(a.get("target"), ref)
    if (target is None or target <= 0
            or (long and target <= ref) or ((not long) and target >= ref)):
        mult = float(config.COPY["atr_target_mult"])
        if atr:
            target = ref + mult * atr if long else ref - mult * atr
        else:
            target = ref + 2.0 * risk_dist if long else ref - 2.0 * risk_dist
    if (long and target < ref + min_tgt) or ((not long) and target > ref - min_tgt):
        return "rejected", f"{sym}: target {target:.2f} sits inside the noise vs live {ref:.2f}"

    # --- size: OUR risk rules, never his size ------------------------------ #
    # A mention carries less information than a stated position, so it is sized
    # down rather than skipped: same rules, smaller budget.
    try:
        risk_mult = max(0.05, min(1.0, float(a.get("risk_mult") or 1.0)))
    except (TypeError, ValueError):
        risk_mult = 1.0
    max_notional = equity * float(config.COPY["max_position_pct"])
    max_risk = equity * float(config.COPY["max_risk_pct"]) * risk_mult
    qty = int(min(max_notional / ref, max_risk / risk_dist))
    if buying_power > 0:
        qty = min(qty, int(buying_power / ref))
    if qty < 1:
        return "rejected", f"{sym}: position would round to zero shares at ${ref:,.2f}"
    reward_dist = abs(target - ref)
    rr = round(reward_dist / risk_dist, 2)

    # --- deterministic gates (same ones the other bots honour) ------------- #
    allowed, why = guards.entry_gate(ACCOUNT)
    if not allowed:
        return "rejected", f"{sym}: {why}"
    fomc, fomc_why = guards.fomc_block_new_overnight()
    if fomc:
        return "rejected", f"{sym}: {fomc_why} — a rate decision can gap through the stop"
    days = int(config.COPY.get("block_earnings_within_days", 0) or 0)
    if days > 0 and sym not in ETF_SYMBOLS:
        try:
            import market_ctx
            hit = (market_ctx.earnings_within(client, [sym], days=days) or {}).get(sym)
            if hit:
                return "rejected", (f"{sym}: reports earnings {hit.get('date')} "
                                    f"(in {hit.get('in_days')}d) — a gap would jump the stop")
        except Exception:                                          # noqa: BLE001
            pass

    size_note = (f"risk ${risk_dist * qty:,.0f} "
                 f"({config.COPY['max_risk_pct']:.1%}"
                 f"{f' x {risk_mult:g}, mention-sized' if risk_mult < 0.999 else ''} cap), "
                 f"notional ${ref * qty:,.0f} ({ref * qty / equity:.0%} of equity)")

    if not live:
        # SHADOW: journal the exact order, place nothing, invent no P/L.
        db.log_event(ACCOUNT, STRATEGY, "shadow",
                     wording.shadow_order(sym, "buy" if long else "sell", qty, ref, stop, target),
                     detail=(f"signal {post['url']} | stop {stop_src} | {size_note} | "
                             f"R:R {rr:.1f} | {a['reason']}"))
        # The stop's origin is repeated in the returned reason because that string
        # is what the dashboard shows against the post: "did he give a stop or did
        # the bot invent one?" is the first thing a reader wants to know.
        return "done", (f"shadow order: {qty} {sym} @ {ref:.2f}, stop {stop:.2f} "
                        f"({stop_src}), target {target:.2f}")

    try:
        order = client.bracket_order(
            sym, qty, "buy" if long else "sell", stop_price=stop, target_price=target,
            time_in_force=str(config.COPY.get("time_in_force", "gtc")))
    except AlpacaError as e:
        return "rejected", f"{sym}: broker refused the order ({e})"

    db.insert_trade(
        account=ACCOUNT, strategy=STRATEGY, symbol=sym, asset_class=_asset_class(sym),
        side="long" if long else "short", qty=qty, entry_price=ref, status="open",
        rr_planned=rr, stop_price=round(stop, 2), target_price=round(target, 2),
        order_id=order.get("id"), client_order_id=order.get("client_order_id"),
        signal_post_id=str(post["post_id"]),
        note=(f"Copied @{post['author']}: {a['reason']}")[:200],
        decision_json=json.dumps({
            "decision": "open",
            "signal": {"post_id": str(post["post_id"]), "url": post["url"],
                       "posted_at": post["posted_at"], "text": (post["text"] or "")[:1200]},
            "analysis": a,
            "rationale": a["reason"],
            "rationale_source": "x_post",
            "risk": {"entry_ref": round(ref, 4), "stop": round(stop, 2),
                     "target": round(target, 2), "stop_source": stop_src,
                     "rr": rr, "risk_dollars": round(risk_dist * qty, 2),
                     "notional": round(ref * qty, 2)},
            "context": {"equity": round(equity, 2), "buying_power": round(buying_power, 2),
                        "concurrent_positions": len(positions),
                        "max_position_pct": config.COPY["max_position_pct"],
                        "max_risk_pct": config.COPY["max_risk_pct"]},
        }),
    )
    db.log_event(ACCOUNT, STRATEGY, "go", wording.opened(sym, "buy" if long else "sell", qty),
                 detail=(f"from {post['url']} | live {ref:.2f} stop {stop:.2f} "
                         f"({stop_src}) target {target:.2f} R:R {rr:.1f} | {size_note} | "
                         f"{a['reason']}"))
    print(f"[copy] OPEN {'buy' if long else 'sell'} {sym} x{qty} ref={ref:.2f} "
          f"stop={stop:.2f} target={target:.2f} order={order.get('id')}")
    return "done", f"mirrored {sym}"


def close_position(client: AlpacaClient, post, a: dict, positions: dict,
                   live: bool) -> tuple[str, str]:
    """Mirror an exit he posted. Returns (state, journal_reason)."""
    sym = str(a.get("symbol") or "").strip().upper()
    if not sym:
        return "review", "he said he exited something without naming the instrument"
    if sym not in positions:
        return "rejected", f"{sym}: he exited, but this bot does not hold it"

    if not live:
        db.log_event(ACCOUNT, STRATEGY, "shadow",
                     f"Would have: closed {sym}",
                     detail=f"signal {post['url']} | {a['reason']}")
        return "done", f"shadow close of {sym}"
    try:
        _, released = client.release_and_close(sym)
    except AlpacaError as e:
        return "rejected", f"{sym}: close failed ({e})"
    db.log_event(ACCOUNT, STRATEGY, "exit", wording.closed(sym, "he posted an exit"),
                 detail=(f"from {post['url']} | released {released} bracket leg(s) | "
                         f"{a['reason']}"))
    print(f"[copy] CLOSE {sym} (released {released})")
    return "done", f"closed {sym}"


# --------------------------------------------------------------------------- #
# Cycle
# --------------------------------------------------------------------------- #
def _journal_new_post(p) -> None:
    db.log_event(ACCOUNT, STRATEGY, "post",
                 wording.new_post(p["author"], p["text"] or "(media only)"),
                 detail=(f"{p['url']} | published {p['posted_at']} | source {p['source']}"
                         + (" | has media" if p["has_media"] else "")))


def _park_stale_new() -> int:
    """Posts the LLM has failed to read for too long become 'review' items.

    Without this a persistently unreadable post is retried on every poll forever
    and the failure stays invisible; parking it puts it in front of the operator.
    """
    parked = 0
    for row in db.x_posts(limit=200, states=("new",)):
        age = _age_min(row["seen_at"])
        if age is not None and age > _NEW_POST_TIMEOUT_H * 60:
            db.set_x_post(row["post_id"], state="review",
                          reason="could not be read automatically — needs a look")
            db.log_event(ACCOUNT, STRATEGY, "error",
                         f"Could not read his post after {_NEW_POST_TIMEOUT_H:g}h "
                         "— parked for review",
                         detail=f"{row['url']} | state=review")
            parked += 1
    return parked


def run(dry_run: bool = False, handle: str | None = None) -> None:
    db.init_db()
    feed = config.X_FEED
    handle = (handle or feed["handle"]).lstrip("@")
    live = config.copy_account_configured()

    dclient = data_client()
    if dclient is None:
        db.log_event(ACCOUNT, STRATEGY, "error",
                     "no usable Alpaca key — cannot read prices or place orders",
                     dedup=True)
        print("[copy] no usable Alpaca key; standing down")
        return

    # ---------------- 1. poll the feed -------------------------------------- #
    # Usually a walk back to the newest post we already hold (one call). Every
    # `sweep_interval_h` hours it walks deeper instead, as insurance against a
    # post that reached the search index late or a poller that was down.
    mode = "trigger"
    last_sweep = db.feed_state_get("last_sweep")
    sweep_age_h = _hours_since(last_sweep)
    if sweep_age_h is None or sweep_age_h >= float(feed.get("sweep_interval_h", 6)):
        mode = "sweep"
    known = {str(r["post_id"]) for r in db.x_posts(limit=1000)}
    res = xfeed.fetch_posts(handle=handle, known_ids=known, mode=mode)
    if res["error"]:
        # LOUD. This is the one failure that must never look like a quiet day.
        db.feed_state_set("last_error", str(res["error"]))
        db.feed_state_set("last_error_at", _now().isoformat())
        db.log_event(ACCOUNT, STRATEGY, "error", f"X feed could not be read: {res['error']}")
        print(f"[copy] feed error: {res['error']}")
        return
    if mode == "sweep":
        db.feed_state_set("last_sweep", _now().isoformat())
    db.feed_state_set("last_poll", _now().isoformat())
    db.feed_state_set("last_provider", f"{res['provider']}/{mode}")
    if res.get("gap_suspect"):
        # The walk ran out of timeline before reaching a post we already held.
        # We cannot tell whether that means "start of the account" or "the index
        # is hiding something", so it is reported rather than assumed away.
        db.log_event(ACCOUNT, STRATEGY, "error",
                     "the feed may have a gap — it walked back without reaching a "
                     "post we already had; the next sweep will re-check",
                     detail=f"mode={mode} calls={res['calls']}")

    # A successful poll clears the error state, so what /health and the watchers
    # report is always the LATEST outcome rather than a scar from an outage that
    # already healed. The failure itself stays in the journal.
    if db.feed_state_get("last_error"):
        db.feed_state_set("last_error", "")
        db.feed_state_set("last_error_at", "")

    new_posts = []
    for p in res["posts"]:
        fresh = db.upsert_x_post(
            p["id"], p["author"], url=p["url"], posted_at=p["posted_at"],
            text=p["text"], is_reply=int(p["is_reply"]), has_media=int(p["has_media"]),
            source=p["source"])
        if fresh:
            new_posts.append(p)
    # Oldest first: trades must be considered in the order he published them.
    new_posts.sort(key=lambda p: (p["posted_at"], p["id"]))
    for p in new_posts:
        _journal_new_post(p)
    print(f"[copy] feed {res['provider']}/{mode}: {len(res['posts'])} post(s) seen, "
          f"{len(new_posts)} new, {res['calls']} call(s) ({'LIVE' if live else 'SHADOW'})")

    _park_stale_new()

    # ---------------- 2. read the unread posts ------------------------------ #
    pending = [r for r in db.x_posts(limit=50, states=("new",))]
    pending.reverse()                       # oldest first
    for row in pending:
        if dry_run:
            print(f"[copy] (dry-run) would read {row['post_id']}")
            continue
        try:
            a = analyse(row)
        except Exception as e:                                     # noqa: BLE001
            db.log_event(ACCOUNT, STRATEGY, "error", f"Could not read his post: {e}",
                         detail=row["url"])
            print(f"[copy] analyse error {row['post_id']}: {e}")
            continue

        state, reason = _disposition(row, a)
        db.set_x_post(row["post_id"], state=state, kind=a["kind"], symbol=a["symbol"],
                      side=a["side"], confidence=a["confidence"], reason=reason,
                      analysis_json=json.dumps(a))
        # The journal is the audit trail, so a post that produced NO trade still
        # gets a line saying what the bot concluded and why. Without this the log
        # would show "New post" and then silence, and "why didn't it copy that?"
        # would be unanswerable from the record.
        if state in ("ignored", "review"):
            db.log_event(ACCOUNT, STRATEGY,
                         "read" if state == "ignored" else "review",
                         wording.signal_read(a["kind"], reason),
                         detail=f"{row['url']} | confidence {a['confidence']:.0%}")
        print(f"[copy] {row['post_id']} -> {state}: {reason}")

    # ---------------- 3. act on what is waiting ----------------------------- #
    positions: dict = {}
    equity, buying_power = 0.0, 0.0
    try:
        if live:
            acct = dclient.account()
            equity = float(acct.get("equity") or 0.0)
            buying_power = float(acct.get("buying_power") or 0.0)
            positions = {p["symbol"]: p for p in dclient.positions()}
        else:
            # Shadow mode still sizes off a real equity figure so the journal
            # shows the position the bot would actually take, and it tracks its
            # own book from the posts (no trade rows, so nothing to invent).
            # A copy account that does not exist yet is sized as if it had its
            # own starting capital — never another bot's equity, which would
            # conflate two books.
            equity = float(db.account_equity().get(ACCOUNT)
                           or config.STARTING_CAPITAL.get(ACCOUNT, 10000.0))
            positions = shadow_book()
    except AlpacaError as e:
        db.log_event(ACCOUNT, STRATEGY, "error", f"account lookup failed: {e}", dedup=True)
        return
    if equity <= 0:
        equity = float(config.STARTING_CAPITAL.get(ACCOUNT, 10000.0))
    buying_power = buying_power or equity

    # Is the market open? A post is a signal, not a fill: an entry that arrives
    # while the market is shut waits for the open instead of chasing.
    market_open = False
    try:
        market_open = bool(dclient.clock().get("is_open"))
    except AlpacaError as e:
        db.log_event(ACCOUNT, STRATEGY, "error", f"clock lookup failed: {e}", dedup=True)
        return

    actionable = list(db.x_posts(limit=50, states=("open", "exit")))
    actionable.reverse()
    for row in actionable:
        age = _age_min(row["posted_at"] or row["seen_at"])
        # A mention is a weaker signal, so it also goes stale sooner: what he was
        # interested in last week is not what he is interested in now.
        age_key = ("mention_max_age_min" if str(row["kind"]) == "interest"
                   else "max_signal_age_min")
        max_age = float(config.COPY.get(age_key, 720))
        if age is None or age > max_age:
            db.set_x_post(row["post_id"], state="rejected",
                          reason=f"too old to mirror ({age if age is None else round(age)} min)")
            db.log_event(ACCOUNT, STRATEGY, "no_go",
                         f"Ignored his {row['kind']} signal for {row['symbol']} — it is "
                         f"{round(age or 0)} min old (limit {round(max_age)} min)")
            continue
        if row["kind"] == "close":
            if not market_open:
                db.log_event(ACCOUNT, STRATEGY, "skip",
                             f"Exiting {row['symbol']} at the next open — the market is shut",
                             dedup=True)
                continue
            try:
                a = json.loads(row["analysis_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                a = {}
            state, reason = close_position(dclient, row, a, positions,
                                           live and not dry_run)
            if state == "done":
                db.set_x_post(row["post_id"], state="done", reason=reason)
                positions.pop(str(row["symbol"] or "").upper(), None)
            else:
                # A close we did NOT mirror is still a decision, and the journal
                # is the audit trail. Without a line here the record shows "New
                # post from @…: full ported $AMZN today…" and then silence, so
                # "why didn't it follow his exit?" has no answer (observed
                # 2026-10-01: the post was read, dispositioned and never
                # journaled — 14 posts, 13 journal lines).
                db.set_x_post(row["post_id"],
                              state="review" if state == "review" else "rejected",
                              reason=reason)
                if not any(str(e["reason"] or "").startswith(reason[:40])
                           for e in db.recent_events(5, account=ACCOUNT)):
                    db.log_event(ACCOUNT, STRATEGY,
                                 "review" if state == "review" else "no_go",
                                 f"Did not follow his exit of {row['symbol']} — {reason}",
                                 detail=row["url"])
                print(f"[copy] did not follow his exit {row['symbol']}: {reason}")
            continue

        # entry
        try:
            a = json.loads(row["analysis_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            a = {}
        syms = _entry_symbols(row, a)
        if not syms:
            db.set_x_post(row["post_id"], state="review",
                          reason="no tradable ticker to act on")
            continue
        if not market_open:
            db.log_event(ACCOUNT, STRATEGY, "skip",
                         f"Holding his {', '.join(syms)} call until the market opens",
                         detail=row["url"], dedup=True)
            continue
        risk_mult = _entry_risk_mult(row["kind"])
        results = []
        for sym in syms:
            per = dict(a, symbol=sym, risk_mult=risk_mult)
            st, rs = open_position(dclient, row, per, equity, buying_power,
                                   positions, live and not dry_run)
            results.append((sym, st, rs))
            if st == "done":
                positions[sym] = {"symbol": sym}
        acted = [s for s, st, _ in results if st == "done"]
        # Which names were actually entered, recorded so the shadow book (and
        # anyone reading the row) sees positions rather than his reading list.
        a["acted"] = acted
        db.set_x_post(row["post_id"], analysis_json=json.dumps(a))
        if acted:
            db.set_x_post(row["post_id"], state="done",
                          reason="; ".join(rs for _, _, rs in results))
        else:
            state = results[0][1]
            reason = "; ".join(f"{s}: {r}" for s, _, r in results)
            db.set_x_post(row["post_id"], state=state, reason=reason)
            if state == "rejected":
                # Not "go": say plainly why we are not following him this time.
                if not any(str(e["reason"] or "").startswith(reason[:40])
                           for e in db.recent_events(5, account=ACCOUNT)):
                    db.log_event(ACCOUNT, STRATEGY, "no_go",
                                 f"Did not follow his {row['symbol']} call — {reason}",
                                 detail=row["url"])
                print(f"[copy] rejected {row['symbol']}: {reason}")

    print(f"[copy] cycle done (equity={equity:,.2f}, "
          f"{len(positions)} mirrored position(s))")


def _disposition(row, a: dict) -> tuple[str, str]:
    """Map an LLM read of a post onto a stored state + human-readable reason."""
    kind = a["kind"]
    if kind == "none":
        return "ignored", a["reason"]
    if kind == "unknown" or not a.get("symbol"):
        if a.get("needs_media"):
            return "review", ("the position is only in the attached image — "
                              f"{a['reason']}")
        return "review", a["reason"]
    if kind == "interest":
        # A name he likes is a weaker signal than a position he took, so it gets
        # its own (lower) bar and its own size, and the reason says so — a reader
        # must never have to guess whether a trade came from a call or a mention.
        if not config.COPY.get("follow_mentions", True):
            return "ignored", ("following the names he mentions is switched off "
                               f"in the config ({a['reason']})")
        if a.get("side") == "short":
            # Long-only for mentions: "he is negative about X" is a view, not a
            # name he is interested in, and shorting on a passing remark is two
            # speculative steps stacked on each other.
            return "ignored", ("he is not positive about "
                               f"{', '.join(a.get('symbols') or [a['symbol']])} — "
                               f"mentions are followed long only ({a['reason']})")
        if a["confidence"] < float(config.COPY.get("mention_min_confidence", 0.45)):
            return "ignored", (f"mention is too vague to act on "
                               f"({a['confidence']:.0%}) — {a['reason']}")
        return "open", (f"he named {', '.join(a.get('symbols') or [a['symbol']])} "
                        f"without claiming a position — {a['reason']}")
    if a["confidence"] < float(config.COPY.get("min_confidence", 0.5)):
        return "ignored", (f"not confident enough to act ({a['confidence']:.0%}): "
                           f"{a['reason']}")
    if kind == "trim":
        # A partial exit would mean replacing the bracket on a live position;
        # v1 leaves the stop and target in charge and says so out loud.
        return "ignored", (f"he trimmed rather than exited ({a['reason']}) — this bot "
                           "mirrors full exits only, so its stop and target stay in charge")
    if kind == "close":
        if not config.COPY.get("follow_exits", True):
            return "ignored", "exit following is switched off in the config"
        return "exit", a["reason"]
    return "open", a["reason"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--handle", default=None)
    args = ap.parse_args()
    run(dry_run=args.dry_run, handle=args.handle)


if __name__ == "__main__":
    main()
