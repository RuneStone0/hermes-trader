"""Technical indicators used by the strategies and the backtester.

Pure functions over lists of OHLCV bars (dicts with 'o','h','l','c','v' keys).
No I/O, no LLM — cheap, deterministic, and unit-testable.
"""
from __future__ import annotations

Bar = dict


def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average; None until enough data."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def true_range(bars: list[Bar]) -> list[float]:
    tr = [0.0] * len(bars)
    for i, b in enumerate(bars):
        h, l = b["h"], b["l"]
        if i == 0:
            tr[i] = h - l
        else:
            pc = bars[i - 1]["c"]
            tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    return tr


def atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    """Average True Range (Wilder's smoothing). None until warm-up."""
    n = len(bars)
    out: list[float | None] = [None] * n
    if n == 0:
        return out
    tr = true_range(bars)
    if n < period:
        return out
    out[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def pct_change(values: list[float]) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(1, len(values)):
        if values[i - 1]:
            out[i] = (values[i] - values[i - 1]) / values[i - 1]
    return out
