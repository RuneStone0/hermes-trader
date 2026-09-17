"""Account-level risk governor — deterministic, state-based, self-recovering.

WHY THIS EXISTS
---------------
The first version of "risk management" here was flinch-based: the nightly
self-improvement routine watched the win/loss streak and shrank position size
every time the bot lost. Measured on 2026-09-17 that had produced:

    YOLO max_position_pct  0.60 -> 0.25 -> 0.10 -> 0.05   (three cuts)
    YOLO max_risk_pct      0.05 -> 0.02 -> 0.01
    YOLO max_concurrent    10   -> 3    -> 1

...on the evidence of 5, 6 and 7 closed trades. That is noise-fitting, and it
is one-directional: nothing in the loop could ever raise the limits again, so
an unproven-but-alive strategy was squeezed to a $500-max-exposure, one-position
bot that risks ~$12-25 a trade (about 0.1-0.25% of a $10k account). It cannot
win or lose meaningfully, and a strategy that cannot be measured cannot be
improved. The routine's OWN first lesson said "do not tune until at least 30
closed paper trades" — and then it tuned three times below that gate.

The correct instrument is DRAWDOWN, not a losing streak:

  * A losing streak is expected variance for any positive-expectancy system.
    At a 2:1 reward:risk you need only ~33% wins, so seven losses in a row
    happens about 6% of the time for a GOOD system. Reacting to it is reacting
    to noise.
  * A drawdown below the account's high-water mark is the actual damage
    signal, and it is a pure function of current state — so it de-risks fast
    AND re-risks automatically as equity recovers. No ratchet, no flinch.

This module is deliberately deterministic (zero tokens): it reads the equity
history reconcile already records and returns a size multiplier plus a
new-entries gate. Every strategy multiplies its own size by
`size_multiplier()` and checks `can_open()` before submitting a NEW entry.

FLOOR POLICY: the governor may only ever REDUCE risk. `RISK_GOV` is not in the
auto-tune whitelist and must never be — an autonomous loop that can loosen its
own risk brakes is not self-improvement, it is a way to lose the account. The
nightly routine can PROPOSE governance changes (they land in
strategy_proposals.md for review); it cannot apply them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
import db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def peak_equity(account: str) -> float | None:
    """Highest equity this account has ever been recorded at.

    The floor is the account's starting capital: an account that has only ever
    lost money must still measure drawdown from its starting line, otherwise
    the first equity point *becomes* the high-water mark and a bot that has
    quietly bled 8% looks flat.
    """
    state = db.account_state().get(account) or {}
    start = state.get("starting_equity") or config.STARTING_CAPITAL.get(account)
    hist = db.equity_peak(account)
    candidates = [v for v in (start, hist) if v]
    return max(candidates) if candidates else None


def current_equity(account: str) -> float | None:
    return (db.account_state().get(account) or {}).get("equity")


def drawdown_pct(account: str) -> float | None:
    """Current drawdown from the high-water mark, in percent (0.0 = at highs)."""
    peak, eq = peak_equity(account), current_equity(account)
    if not peak or not eq or peak <= 0:
        return None
    return max(0.0, (peak - eq) / peak * 100.0)


def _band_multiplier(dd: float) -> float:
    """Map a drawdown % to a size multiplier using config.RISK_GOV['dd_bands'].

    Bands are ordered (max_dd_pct, multiplier) and the FIRST band whose ceiling
    the drawdown fits inside wins, so the deepest band is an implicit floor.
    """
    for ceiling, mult in config.RISK_GOV["dd_bands"]:
        if dd <= ceiling:
            return float(mult)
    return float(config.RISK_GOV["dd_bands"][-1][1])


def daily_pnl_pct(account: str) -> float | None:
    """Today's P/L as a % of the prior close, including OPEN positions.

    Alpaca reports `last_equity` (the previous session's close) on the account
    object, and reconcile snapshots it every 10 minutes, so equity-vs-last_equity
    gives a true day P/L — realized AND unrealized — with no extra broker call.
    Falls back to realized-only (closed-trade net P/L today, ET) when the
    snapshot is missing, which is the conservative direction.
    """
    state = db.account_state().get(account) or {}
    eq, last = state.get("equity"), state.get("last_equity")
    if eq and last and last > 0:
        return (eq - last) / last * 100.0
    realized = db.realized_pnl_today(account)
    if realized is None or not eq:
        return None
    return realized / eq * 100.0


def size_multiplier(account: str) -> tuple[float, str]:
    """(multiplier, reason) for NEW position sizing. Never increases risk."""
    if not config.RISK_GOV.get("enabled", True):
        return 1.0, ""
    dd = drawdown_pct(account)
    if dd is None:
        return 1.0, ""          # no state yet -> fail open, first trades run full size
    mult = _band_multiplier(dd)
    if mult >= 1.0:
        return 1.0, ""
    return mult, f"drawdown {dd:.1f}% below the account high-water mark"


def can_open(account: str) -> tuple[bool, str]:
    """Gate for NEW entries. (allowed, reason). Reducing-only, never raising."""
    gov = config.RISK_GOV
    if not gov.get("enabled", True):
        return True, ""

    dd = drawdown_pct(account)
    if dd is not None and dd > gov["hard_stop_dd_pct"]:
        return False, (f"drawdown {dd:.1f}% beyond the {gov['hard_stop_dd_pct']:.0f}% "
                       f"hard stop — managing existing positions only")

    day = daily_pnl_pct(account)
    cap = gov.get("daily_loss_cap_pct")
    if day is not None and cap is not None and day <= -abs(cap):
        return False, (f"daily loss {day:.2f}% hit the {abs(cap):.2f}% cap — "
                       f"no new entries until the next session")

    # Concurrent-exposure sanity: total notional across open positions vs equity.
    exp = exposure_pct(account)
    max_exp = gov.get("max_gross_exposure_pct")
    if exp is not None and max_exp is not None and exp > max_exp:
        return False, f"gross exposure {exp:.0f}% of equity over the {max_exp:.0f}% ceiling"
    return True, ""


def exposure_pct(account: str) -> float | None:
    """Gross open-position notional as a % of equity (from the trades table)."""
    eq = current_equity(account)
    if not eq or eq <= 0:
        return None
    gross = 0.0
    for t in db.open_trades(account):
        px = t["last_price"] or t["entry_price"] or 0.0
        gross += abs(float(px) * float(t["qty"] or 0))
    return gross / eq * 100.0


def status(account: str) -> dict:
    """Full governor state — used by the dashboard, the journal and the LLM
    context so a bot can always see WHY its size was trimmed."""
    mult, reason = size_multiplier(account)
    allowed, gate = can_open(account)
    dd = drawdown_pct(account)
    day = daily_pnl_pct(account)
    return {
        "enabled": bool(config.RISK_GOV.get("enabled", True)),
        "drawdown_pct": round(dd, 2) if dd is not None else None,
        "peak_equity": round(peak_equity(account) or 0.0, 2) or None,
        "daily_pnl_pct": round(day, 2) if day is not None else None,
        "exposure_pct": round(exposure_pct(account) or 0.0, 1) if exposure_pct(account) is not None else None,
        "size_multiplier": round(mult, 3),
        "size_reason": reason,
        "can_open": allowed,
        "gate_reason": gate,
        "text": _text(mult, reason, allowed, gate, dd, day),
    }


def _text(mult: float, reason: str, allowed: bool, gate: str,
          dd: float | None, day: float | None) -> str:
    bits = []
    bits.append(f"drawdown {dd:.1f}%" if dd is not None else "drawdown n/a")
    if day is not None:
        bits.append(f"today {day:+.2f}%")
    if not allowed:
        bits.append(f"NEW ENTRIES BLOCKED: {gate}")
    elif mult < 1.0:
        bits.append(f"sizing at {mult:.0%} ({reason})")
    else:
        bits.append("sizing at full allowed size")
    return "; ".join(bits)


def ban_days_since_peak(account: str, days: int = 999) -> int | None:
    """Sessions since the equity high-water mark (diagnostic/dashboard helper)."""
    eq = current_equity(account)
    if eq is None:
        return None
    rows = db.equity_series(account, days=days)
    if not rows:
        return None
    best = max(rows, key=lambda r: r["equity"] or float("-inf"))
    try:
        ts = datetime.fromisoformat(str(best["ts"]).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return max(0, int((_now() - ts).total_seconds() // 86400))
