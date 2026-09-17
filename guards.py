"""Shared decision gates + context for all three bots.

WHY THIS MODULE EXISTS
----------------------
The three strategies had drifted into three slightly different worlds: the daily
and weekly bots decided from a handful of numbers and a hardcoded "no news feed";
the autonomous bot decided from 30 daily closes per symbol. Every one of them was
blind to the same three things — the macroeconomic calendar, the news tape, and
the account's own drawdown — and each wired its LLM prompt by hand.

This module is the single place those inputs are assembled, so the bots stay
UNIFORM (the user's standing requirement is that the whole app behaves the same
way everywhere) and so a fix here lands in all three bots at once:

  * entry_gate()   — deterministic pre-trade block: risk governor + event blackout
  * size_factor()  — the combined size multiplier (drawdown band x event day)
  * context()      — the shared context block merged into every LLM decision
  * framing()      — the anti-doom-loop framing (a streak is variance; flat is a
                     position that is being measured against the market)

EVERYTHING FAILS OPEN. A missing calendar, a news outage, an absent market_ctx
module: each degrades to "no extra information" and the bot trades exactly as it
did before. A data source must never be able to halt trading.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
import db
import risk_gov


def _log(account: str, strategy: str, decision: str, reason: str,
         detail: str | None = None) -> None:
    try:
        db.log_event(account, strategy, decision, reason, detail=detail)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Deterministic gates
# --------------------------------------------------------------------------- #
def entry_gate(account: str) -> tuple[bool, str]:
    """May a NEW position be opened right now? (allowed, reason).

    Two independent checks, both reducing-only:
      1. the drawdown governor (dd / daily-loss cap / exposure ceiling)
      2. the economic-calendar blackout around a high-impact US release
    The calendar check is skipped entirely when EVENT_GUARD is disabled or the
    feed is unavailable (fail open).
    """
    allowed, reason = risk_gov.can_open(account)
    if not allowed:
        return False, reason

    guard = config.EVENT_GUARD
    if guard.get("enabled", True):
        try:
            import econ
            blocked, why = econ.blackout(pad_min=guard["blackout_min"],
                                        min_importance=guard["blackout_importance"],
                                        countries=tuple(guard.get("countries", ("US",))))
            if blocked:
                return False, f"econ blackout: {why}"
        except Exception:
            pass
    return True, ""


def size_factor(account: str) -> tuple[float, list[str]]:
    """Combined NEW-position size multiplier: drawdown band x event-day cut.

    Returns (factor, reasons). Never exceeds 1.0 — nothing here can size UP.
    """
    factor = 1.0
    reasons: list[str] = []

    mult, why = risk_gov.size_multiplier(account)
    if mult < 1.0:
        factor *= mult
        reasons.append(f"{mult:.0%} for {why}")

    guard = config.EVENT_GUARD
    if guard.get("enabled", True):
        try:
            import econ
            on_event, what = econ.event_day(min_importance=guard.get("event_day_importance", 0),
                                            countries=tuple(guard.get("countries", ("US",))))
            if on_event:
                cut = float(guard.get("event_day_size_cut", 1.0))
                factor *= cut
                reasons.append(f"{cut:.0%} on an event day ({what})")
        except Exception:
            pass
    return max(0.0, min(1.0, factor)), reasons


def fomc_block_new_overnight(now=None) -> tuple[bool, str]:
    """Should a NEW position be prevented from holding through today's FOMC
    decision? A rate decision is a coin-flip gap that a stop-loss cannot protect
    (the SLV gap-through already showed the failure mode at -1.74R)."""
    guard = config.EVENT_GUARD
    if not guard.get("enabled", True) or not guard.get("block_overnight_into_fomc", True):
        return False, ""
    try:
        import econ
        hits = [e for e in econ.high_impact_today(now=now,
                                                 countries=tuple(guard.get("countries", ("US",))))
                if "fomc" in (e.get("title") or "").lower()
                or "fed funds" in (e.get("title") or "").lower()
                or "federal funds" in (e.get("title") or "").lower()
                or "fed press conference" in (e.get("title") or "").lower()]
        if hits:
            return True, f"FOMC today: {hits[0]['title']} {hits[0]['ts'][11:16]}Z"
    except Exception:
        pass
    return False, ""


# --------------------------------------------------------------------------- #
# Shared LLM context
# --------------------------------------------------------------------------- #
def context(account: str, client=None, symbols: list[str] | None = None,
            with_news: bool = True, with_symbols: bool = True) -> dict:
    """The shared context block every bot merges into its LLM decision prompt.

    Keys are stable and self-describing so the three prompts read the same:
      risk_governor, economic_calendar, upcoming_events, news, market_regime,
      benchmark, performance_note  (+ 'symbols' richer per-symbol snapshots)
    """
    ctx: dict = {}

    try:
        gov = risk_gov.status(account)
        ctx["risk_governor"] = gov["text"]
        ctx["risk_state"] = {k: gov[k] for k in
                             ("drawdown_pct", "peak_equity", "daily_pnl_pct",
                              "exposure_pct", "size_multiplier", "can_open",
                              "gate_reason")}
    except Exception as e:
        ctx["risk_governor"] = f"unavailable: {e}"

    guard = config.EVENT_GUARD
    try:
        import econ
        if guard.get("enabled", True):
            # The DIGEST is informational, so it carries medium+high events (a
            # Fed speaker can move the tape even though it is not worth resizing
            # for). Only the SIZE CUT is calibrated to high-impact only.
            ctx["economic_calendar"] = econ.brief(
                days=3, countries=tuple(guard.get("countries", ("US",))),
                min_importance=0) or "no notable US events"
            today = econ.high_impact_today(countries=tuple(guard.get("countries", ("US",))))
            if today:
                ctx["high_impact_today"] = [f"{e['title']} {e['ts'][11:16]}Z" for e in today[:6]]
    except Exception as e:
        ctx["economic_calendar"] = f"unavailable: {e}"

    if with_news:
        try:
            import news
            digest = news.brief(client, symbols=symbols, hours=24, limit=12)
            if digest:
                ctx["news"] = digest
        except Exception:
            pass

    try:
        import market_ctx
        if client is not None:
            reg = market_ctx.regime(client)
            ctx["market_regime"] = reg.get("text") or reg.get("label")
            ctx["regime_label"] = reg.get("label")
            if with_symbols and symbols:
                ctx["symbols"] = {s: market_ctx.symbol_snapshot(client, s) for s in symbols}
            if with_symbols and symbols:
                ctx["earnings"] = market_ctx.earnings_within(client, symbols, days=10)
    except Exception as e:
        ctx.setdefault("market_regime", f"unavailable: {e}")

    bench = benchmark_note(account)
    if bench:
        ctx["benchmark"] = bench

    note = framing(account)
    if note:
        ctx["performance_note"] = note
    return ctx


def benchmark_note(account: str) -> str:
    """'you are +X% since inception, SPY is +Y% over the same window'.

    Measuring a bot against ZERO is how a flat tape flatters it: in Sep 2026 the
    daily bot looked fine at +$9.27 while SPY was -0.06% — the honest comparison.

    The window MUST start at the account's own inception (its first trade), not
    at the start of the cached benchmark series: comparing a two-week-old account
    against SPY's 9-month return reads as "we are losing to the market by 13
    points" when the market did nothing over the period the account actually
    traded. Verified live 2026-09-17: the naive version printed SPY +11.55% for
    an account that had been open twelve sessions (SPY over that window: -0.06%).
    Returns '' when there is not enough data to say anything real.
    """
    try:
        state = db.account_state().get(account) or {}
        eq, start = state.get("equity"), state.get("starting_equity")
        if not eq or not start:
            return ""
        mine = (eq / start - 1) * 100.0
        series = db.benchmark_series("SPY", days=400)
        if len(series) < 2:
            return f"account {mine:+.2f}% vs starting capital"
        rows = db.all_trades(account)
        inception = min((t["created_at"] for t in rows), default=None) if rows else None
        if inception is None:
            # No trades yet => no trading window. Fall back to the earliest
            # equity point we have for this account (equity_history starts when
            # reconcile first snapshotted it), and if even that is missing, say
            # nothing about the market rather than borrow SPY's return over a
            # window the account was not open for. Observed live 2026-09-17: an
            # account with zero trades reported "vs SPY +11.60%" (nine months of
            # S&P return) beside its own +0.00%.
            points = db.equity_series(account, days=400)
            inception = points[0]["ts"] if points else None
        if inception is None:
            return f"account {mine:+.2f}% (no trades yet)"
        day = str(inception)[:10]
        window = [p for p in series if p["d"] >= day] or series
        if len(window) < 2 or not window[0]["close"]:
            return f"account {mine:+.2f}% vs starting capital"
        spy = (window[-1]["close"] / window[0]["close"] - 1) * 100.0
        return (f"account {mine:+.2f}% since inception vs SPY buy-and-hold "
                f"{spy:+.2f}% over the same window ({window[0]['d']} to {window[-1]['d']})")
    except Exception:
        return ""


def framing(account: str) -> str:
    """The anti-doom-loop paragraph.

    On 2026-09-15/16 five consecutive YOLO cycles returned the same decision —
    "Holding: 0/7 recent losses, preserve capital" — while the bot owned only
    its own losing streak as evidence. A streak is not a reason to stop trading:
    at 2:1 reward:risk a profitable system needs only ~33% winners and will
    still see seven losses in a row about 6% of the time. Being told that
    explicitly is the difference between a system that adapts and one that
    talks itself into permanent cash.
    """
    try:
        closed = [t for t in db.all_trades(account) if t["status"] == "closed"]
    except Exception:
        return ""
    if not closed:
        return ""
    tail = closed[-8:]
    losses = sum(1 for t in tail if (t["net_pnl"] or 0) <= 0)
    wins = len(tail) - losses
    parts = [
        f"Your last {len(tail)} closed trades: {wins}W/{losses}L.",
        "A losing streak is not evidence that the strategy is broken: at 2:1 "
        "reward:risk a genuinely profitable system wins only ~1 in 3 and will "
        "still lose 7 in a row about 6% of the time. Do NOT refuse to trade "
        "merely because of a streak — decide from the setup in front of you.",
    ]
    # Where a winner SHOULD have paid, if the sample is all stops, say so: it
    # locates the problem (entry timing / stop distance) rather than the outcome.
    reasons = [(t["close_reason"] or "") for t in tail]
    if reasons and all(r == "stop" for r in reasons):
        parts.append("Every one of those exits was a stop-loss, none reached "
                     "+1R, and none hit its target: that points at ENTRY TIMING "
                     "and stop distance, not at the size of the position.")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Journal helpers
# --------------------------------------------------------------------------- #
def journal_gate(account: str, strategy: str, allowed: bool, reason: str) -> None:
    """Record a gate refusal in the decision journal (so the dashboard shows
    WHY the bot stood down, not just that it did)."""
    if not allowed and reason:
        _log(account, strategy, "blocked", f"Standing down: {reason}")


def note_size_adjust(account: str, strategy: str, factor: float,
                     reasons: list[str]) -> None:
    if factor < 1.0 and reasons:
        _log(account, strategy, "skip",
             f"Sizing at {factor:.0%} — " + "; ".join(reasons))
