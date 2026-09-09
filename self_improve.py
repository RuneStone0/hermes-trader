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
        out[a] = {
            "closed": n,
            "win_rate": round(wins / n, 3) if n else 0.0,
            "net_pnl": round(sum(t["net_pnl"] or 0 for t in closed), 2),
            "fees": round(sum(t["fees"] or 0 for t in closed), 2),
        }
    return out


def _tail(path, n: int = 2000) -> str:
    if not path.exists():
        return ""
    return path.read_text()[-n:]


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
        "Respond with ONLY a JSON object (no markdown):\n"
        '{"lessons":[{"topic":"...","detail":"..."}],'
        '"proposals":[{"title":"...","rationale":"...","params":{...}}]}'
    )
    user = json.dumps({
        "performance": perf,
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

    # --- 3. auto-apply tuned knobs (within floor) ---
    applied = bool(clean)
    if applied:
        _write_overrides(clean)
        for sec, vals in clean.items():
            for k, v in (vals.items() if isinstance(vals, dict) else [(sec, vals)]):
                applied_details.append(f"{sec}.{k}={v}")
        db.log_event("improve", "self_improve", "apply",
                     f"auto-applied {len(applied_details)} tuned knob(s)",
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
    if pending:
        block += [f"\n## {stamp} — PENDING REVIEW (structural, no auto-apply)"]
        for p in pending:
            block.append(f"- **{p.get('title', 'proposal')}** — {p.get('rationale', '')}")
    if block:
        with open(config.PROPOSALS_PATH, "a") as f:
            f.write("\n".join(block) + "\n")

    print(f"[improve] appended {len(lessons)} lessons; "
          f"auto-applied {len(applied_details)} knob(s); "
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
