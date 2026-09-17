"""Nightly self-improvement — self-contained (no Hermes).

Reviews closed-trade performance plus the existing lessons/proposals notes, then
asks the LLM for:
  - operational lessons      -> appended to lessons_learned.md
  - strategy changes         -> TWO kinds:
      (a) TUNE-ABLE params (map to the config auto-tune whitelist) are
          VALIDATED against the safety floor and AUTO-APPLIED (written to
          strategy_overrides.json, which every run script re-imports).
      (b) STRUCTURAL proposals (new rules/filters) have no safe param mapping
          -> appended to strategy_proposals.md as PENDING REVIEW.

Safety: the routine can tune ONLY whitelisted knobs within their allowed bounds
(config.propose_override clamps everything and drops the irreducible floor —
stop-loss / retail-only / BTC-only can NEVER be disabled here). Every applied
change is journaled so the system stays auditable. An unattended container CAN
now evolve its own strategy parameters, but never its own safety floor.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import advisor
import config
import db

ACCOUNTS = ("daily", "weekly", "yolo")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _perf_summary() -> dict:
    trades = db.all_trades()
    out: dict = {}
    for a in ACCOUNTS:
        closed = [t for t in trades if t["account"] == a and t["status"] == "closed"]
        n = len(closed)
        wins = sum(1 for t in closed if (t["net_pnl"] or 0) > 0)
        # R-multiple, not dollars: on a $10k paper account "$20" says nothing
        # about whether the ENTRY was any good — the agent's own risk per trade
        # varied by 10x across the first seven trades. R normalises every trade
        # to "how many times my initial risk did I make or lose", which is the
        # only number that can be averaged meaningfully across different sizes.
        rs: list[float] = []
        for t in closed:
            risk = _risk_dollars(t)
            if risk and risk > 0 and t["net_pnl"] is not None:
                rs.append(float(t["net_pnl"]) / risk)
        try:
            import risk_gov
            gov = risk_gov.status(a)
        except Exception as e:                                    # never block the review
            gov = {"error": str(e)}
        out[a] = {
            "closed": n,
            "win_rate": round(wins / n, 3) if n else 0.0,
            "net_pnl": round(sum(t["net_pnl"] or 0 for t in closed), 2),
            "fees": round(sum(t["fees"] or 0 for t in closed), 2),
            "avg_R": round(sum(rs) / len(rs), 3) if rs else None,
            "total_R": round(sum(rs), 2) if rs else None,
            "worst_R": round(min(rs), 2) if rs else None,
            "best_R": round(max(rs), 2) if rs else None,
            "reached_1R_win": sum(1 for r in rs if r >= 1.0),
            "stop_out_share": (round(sum(1 for t in closed
                                      if (t["close_reason"] or "") == "stop") / n, 2)
                               if n else None),
            "risk_state": gov,
        }
    return out


def _risk_dollars(t) -> float | None:
    """Initial risk in dollars for a trade row (|entry - stop| x qty)."""
    try:
        e, s, q = t["entry_price"], t["stop_price"], t["qty"]
        if e is None or s is None or not q:
            return None
        return abs(float(e) - float(s)) * abs(float(q))
    except (TypeError, ValueError):
        return None


# Knob section -> the performance bucket whose sample size gates it. Global
# knobs (RISK_PCT_PER_TRADE / MIN_NET_RR) are gated on the TOTAL across buckets.
_SECTION_BUCKET = {"": "__total__", "DAILY": "daily", "WEEKLY": "weekly", "YOLO": "yolo"}


def _tuning_gate(clean: dict, perf: dict) -> tuple[bool, str]:
    """The mechanical sample gate. Below MIN_CLOSED_TRADES_TO_TUNE closed trades
    in the relevant bucket, NO knob may be auto-applied.

    This exists because the routine's own first lesson (2026-09-01) said exactly
    that — "do not tune any rule until at least 30 closed paper trades are
    logged" — and it then auto-applied three size reductions on the evidence of
    5, 6 and 7 trades, each time reasoning about a losing streak that any 2:1
    system produces ~6% of the time by chance. Proposals and lessons still get
    written below the gate; only the APPLY is blocked.
    """
    total = sum(int(perf.get(a, {}).get("closed") or 0) for a in ACCOUNTS)
    need = int(getattr(config, "MIN_CLOSED_TRADES_TO_TUNE", 30))
    blockers: list[str] = []
    for sec in clean:
        bucket = _SECTION_BUCKET.get(sec.upper() if sec else "", "__total__")
        n = total if bucket == "__total__" else int(perf.get(bucket, {}).get("closed") or 0)
        if n < need:
            blockers.append(f"{sec or 'GLOBAL'} ({n}/{need} closed trades)")
    if blockers:
        return False, ("sample gate: " + ", ".join(blockers))
    return True, ""


def _tail(path, n: int = 2000) -> str:
    if not path.exists():
        return ""
    return path.read_text()[-n:]


def _market_context() -> dict:
    """Regime + upcoming-event context for the review, so proposals can be
    regime-aware ('chop: this rule is a trend rule') instead of only
    loss-aware. Entirely best-effort: any failure returns {} and the review
    still runs — the routine must never be blocked by a data source."""
    ctx: dict = {}
    try:
        import market_ctx
        from alpaca_rest import AlpacaClient
        ctx["regime"] = market_ctx.regime(AlpacaClient("daily"))
    except Exception as e:
        ctx["regime"] = f"unavailable: {e}"
    try:
        import econ
        ctx["upcoming_events"] = econ.brief(days=5)
        ctx["today_events"] = [e["title"] for e in econ.high_impact_today()]
    except Exception as e:
        ctx["upcoming_events"] = f"unavailable: {e}"
    return ctx


def _write_overrides(clean: dict) -> None:
    """Atomic merge of `clean` into the override file (existing knobs kept)."""
    existing: dict = {}
    if config.OVERRIDES_PATH.exists():
        try:
            existing = json.loads(config.OVERRIDES_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    # Deep-merge per section (new value wins; untouched knobs preserved).
    merged = dict(existing)
    for sec, vals in clean.items():
        if sec in ("DAILY", "WEEKLY", "YOLO"):
            merged.setdefault(sec, {})
            merged[sec].update(vals)
        else:
            merged[sec] = vals
    config.OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.OVERRIDES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    tmp.replace(config.OVERRIDES_PATH)


def main() -> None:
    db.init_db()
    perf = _perf_summary()
    knobs = config.tunable_knobs()
    ctx = _market_context()

    system = (
        "You are a trading-system self-improvement analyst. Given performance "
        "stats and the current lessons/proposals notes, produce TWO lists:\n"
        " 1. 'lessons': operational observations — what to keep/stop/watch. "
        "Concrete and specific.\n"
        " 2. 'proposals': strategy changes worth evaluating. EACH carries a "
        "'params' object. If the change is a PARAM shift, set params to the "
        "NEW value for any of the tunable knobs below (this gets auto-applied). "
        "If the change is STRUCTURAL (new rule/filter) with no tunable-param "
        "mapping, set params to {} (it stays PENDING REVIEW).\n"
        "You may move knobs within their allowed range. You can NEVER change a "
        "knob outside 'tunable_knobs' — the stop-loss, retail-only and BTC-only "
        "rules are off-limits. Do not propose anything that would violate them.\n"
        "RISK CONTROLS ARE NOT YOURS TO LOOSEN. risk_state below is a "
        "deterministic drawdown governor that already de-risks automatically; do "
        "not propose shrinking size because of a losing streak or a drawdown it "
        "already covers, and never propose a larger max_position_pct/max_risk_pct "
        "while an account is below its high-water mark.\n"
        "STATISTICAL DISCIPLINE — read this carefully:\n"
        " * perf reports avg_R / total_R (risk-multiple) alongside dollars. Judge "
        "the SYSTEM by R, never by dollars: a -$20 loss on $4 of risk is a much "
        "worse trade than a -$60 loss on $40 of risk.\n"
        " * A losing streak is NOT evidence of a broken strategy. At 2:1 "
        "reward:risk a genuinely profitable system only needs ~33% winners and "
        "will still lose 7 in a row roughly 6% of the time. Do not treat a streak "
        "as a signal to cut risk.\n"
        " * A sample below the sample gate cannot support a parameter change at "
        "all; below it, write the change as a PROPOSAL with params {} and let it "
        "stay pending.\n"
        " * Before proposing anything, ask whether the observed result is better "
        "explained by the REGIME (market_context below) than by the rule. Being "
        "stopped out repeatedly in a chop regime is a regime mismatch, not proof "
        "the entry rule is wrong.\n"
        "Respond with ONLY a JSON object (no markdown):\n"
        '{"lessons":[{"topic":"...","detail":"..."}],'
        '"proposals":[{"title":"...","rationale":"...","params":{...}}]}'
    )
    user = json.dumps({
        "performance": perf,
        "sample_gate_closed_trades": config.MIN_CLOSED_TRADES_TO_TUNE,
        "market_context": ctx,
        "existing_lessons": _tail(config.LESSONS_PATH),
        "existing_proposals": _tail(config.PROPOSALS_PATH),
        "tunable_knobs": knobs,
    }, indent=2)

    try:
        content = advisor.chat(system, user, temperature=0.3, max_tokens=4000)
    except Exception as e:
        print(f"[improve] LLM error: {e}")
        db.log_event("improve", "self_improve", "error", f"LLM error: {e}")
        return

    out = advisor._extract_json(content)
    if not isinstance(out, dict):
        print("[improve] unparseable output (no-op)")
        db.log_event("improve", "self_improve", "error", "unparseable LLM output")
        return

    lessons = out.get("lessons") or []
    proposals = out.get("proposals") or []
    stamp = _now()

    # --- 1. lessons (always just appended) ---
    if lessons:
        config.LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        block = [f"\n## {stamp}\n"]
        for l in lessons:
            if isinstance(l, dict):
                block.append(f"- **{l.get('topic', 'note')}**: {l.get('detail', '')}")
        with open(config.LESSONS_PATH, "a") as f:
            f.write("\n".join(block) + "\n")

    # --- 2. proposals: split into auto-apply vs pending-review ---
    candidate: dict = {}
    pending: list[dict] = []
    applied_details: list[str] = []
    for p in proposals:
        if not isinstance(p, dict):
            continue
        params = p.get("params") or {}
        if isinstance(params, dict) and params:
            candidate = _deep_merge(candidate, params)
    clean, notes = config.propose_override(candidate)

    for p in proposals:
        if not isinstance(p, dict):
            continue
        params = p.get("params") or {}
        if isinstance(params, dict) and not params:
            pending.append(p)  # structural — no tunable param -> human review

    # --- 3. auto-apply tuned knobs (within floor AND above the sample gate) ---
    gate_ok, gate_reason = _tuning_gate(clean, perf) if clean else (False, "")
    applied = bool(clean) and gate_ok
    if clean and not gate_ok:
        # The sample gate refused the change. This is a real, journaled outcome
        # (it is the difference between "we chose not to tune" and "we could
        # not tune") — and the proposals still land in strategy_proposals.md
        # below, marked pending rather than silently dropped.
        for p in proposals:
            if isinstance(p, dict) and (p.get("params") or {}):
                pending.append(p)
        db.log_event("improve", "self_improve", "warn",
                     f"Auto-tune held back — {gate_reason}",
                     detail="; ".join(json.dumps(p.get("params") or {}, sort_keys=True)
                                      for p in proposals if isinstance(p, dict)))
    if applied:
        _write_overrides(clean)
        for sec, vals in clean.items():
            for k, v in (vals.items() if isinstance(vals, dict) else [(sec, vals)]):
                applied_details.append(f"{sec}.{k}={v}")
        db.log_event("improve", "self_improve", "apply",
                     f"Auto-tuned {len(applied_details)} setting(s)",
                     detail="; ".join(applied_details))
    for n in notes:
        db.log_event("improve", "self_improve", "warn",
                     f"override note: {n}")

    # --- 4. journal to strategy_proposals.md ---
    config.PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    block: list[str] = []
    if applied:
        block += [f"\n## {stamp} — AUTO-APPLIED (within safety floor)"]
        for p in proposals:
            if isinstance(p, dict) and (p.get("params") or {}):
                block.append(f"- **{p.get('title', 'proposal')}** — {p.get('rationale', '')}")
                if p.get("params"):
                    block.append(f"  `{json.dumps(p['params'], sort_keys=True)}`")
        if notes:
            block.append(f"  *notes: {'; '.join(notes)}*")
    elif clean:
        block += [f"\n## {stamp} — HELD BY SAMPLE GATE (not applied)"]
        block.append(f"- *{gate_reason}* — the change is recorded here but not "
                     f"applied; a sample this small cannot distinguish an edge "
                     f"from variance.")
        for p in proposals:
            if isinstance(p, dict) and (p.get("params") or {}):
                block.append(f"- **{p.get('title', 'proposal')}** — {p.get('rationale', '')}")
                if p.get("params"):
                    block.append(f"  `{json.dumps(p['params'], sort_keys=True)}`")
    if pending:
        block += [f"\n## {stamp} — PENDING REVIEW (structural, no auto-apply)"]
        for p in pending:
            block.append(f"- **{p.get('title', 'proposal')}** — {p.get('rationale', '')}")
    if block:
        with open(config.PROPOSALS_PATH, "a") as f:
            f.write("\n".join(block) + "\n")

    print(f"[improve] appended {len(lessons)} lessons; "
          f"Auto-tuned {len(applied_details)} setting(s); "
          f"{len(pending)} structural proposal(s) left for review")


def _deep_merge(base: dict, new: dict) -> dict:
    out = dict(base)
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


if __name__ == "__main__":
    main()
