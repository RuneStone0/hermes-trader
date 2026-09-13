"""Plain-English wording for the decision journal.

The journal is read by a human skimming the dashboard, so an event's "reason" is
phrased for a non-expert: "Sold short 12 SPY" — not "size_down short SPY x12".
Keeping the phrasing in one place keeps it UNIFORM across all three bots.
"""
from __future__ import annotations

# side -> human verb (daily/weekly use long/short; yolo uses buy/sell).
_VERB = {"buy": "Bought", "long": "Bought", "sell": "Sold short", "short": "Sold short"}


def opened(symbol: str, side: str, qty: int, size_multiplier: float | None = None) -> str:
    """'Bought 12 SPY' / 'Sold short 23 XLU' (+ a note when size was cut)."""
    sym = str(symbol).upper()
    verb = _VERB.get(str(side).lower(), f"Took a {side} position in")
    txt = f"{verb} {qty} {sym}"
    try:
        mult = float(size_multiplier) if size_multiplier is not None else 1.0
    except (TypeError, ValueError):
        mult = 1.0
    if mult < 0.999:
        txt += f" — reduced size ({round(mult * 100)}% of the usual)"
    return txt


def closed(symbol: str, reason: str = "") -> str:
    """'Closed XLP' / 'Closed SPY — stop-loss hit'."""
    sym = str(symbol).upper()
    r = (reason or "").strip()
    return f"Closed {sym}" + (f" — {r}" if r else "")
