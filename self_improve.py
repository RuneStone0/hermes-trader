"""Nightly self-improvement — self-contained (no Hermes).

Reviews closed-trade performance plus the existing lessons/proposals notes, then
asks the LLM for:
  - operational lessons  -> appended to lessons_learned.md
  - strategy proposals   -> appended to strategy_proposals.md (marked PENDING REVIEW)

Safety: this routine NEVER mutates trading code or strategy params. Those are
human-reviewed (or applied by the Hermes monitor). An unattended container must
not self-modify its own trading logic.
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


def main() -> None:
    db.init_db()
    perf = _perf_summary()

    system = (
        "You are a trading-system self-improvement analyst. Given performance "
        "stats and the current lessons/proposals notes, produce TWO lists:\n"
        " 1. 'lessons': operational observations — what to keep doing, stop doing, "
        "or watch for. Concrete and specific.\n"
        " 2. 'proposals': strategy changes worth evaluating (entry rules, filters, "
        "risk sizing, exit management). Do NOT propose anything that would violate "
        "the hard rules (retail instruments only, no crypto except BTC, stop-loss "
        "required, paper-only).\n"
        "Respond with ONLY a JSON object (no markdown):\n"
        '{"lessons":[{"topic":"...","detail":"..."}],'
        '"proposals":[{"title":"...","rationale":"...","params":{...}}]}'
    )
    user = json.dumps({
        "performance": perf,
        "existing_lessons": _tail(config.LESSONS_PATH),
        "existing_proposals": _tail(config.PROPOSALS_PATH),
    }, indent=2)

    try:
        content = advisor.chat(system, user, temperature=0.3, max_tokens=4000)
    except Exception as e:
        print(f"[improve] LLM error: {e}")
        return

    out = advisor._extract_json(content)
    if not isinstance(out, dict):
        print("[improve] unparseable output (no-op)")
        return

    lessons = out.get("lessons") or []
    proposals = out.get("proposals") or []

    stamp = _now()
    if lessons:
        config.LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        block = [f"\n## {stamp}\n"]
        for l in lessons:
            if isinstance(l, dict):
                block.append(f"- **{l.get('topic', 'note')}**: {l.get('detail', '')}")
        with open(config.LESSONS_PATH, "a") as f:
            f.write("\n".join(block) + "\n")

    if proposals:
        config.PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        block = [f"\n## {stamp} — PENDING REVIEW\n"]
        for p in proposals:
            if isinstance(p, dict):
                block.append(f"- **{p.get('title', 'proposal')}** — {p.get('rationale', '')}")
                if p.get("params"):
                    block.append(f"  `{json.dumps(p['params'], sort_keys=True)}`")
        with open(config.PROPOSALS_PATH, "a") as f:
            f.write("\n".join(block) + "\n")

    print(f"[improve] appended {len(lessons)} lessons, {len(proposals)} proposals")


if __name__ == "__main__":
    main()
