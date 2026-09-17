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


def rsi(closes: list[float], period: int = 2) -> list[float | None]:
    """Wilder RSI. out[i] is None until `period` changes are available.

    Copied deliberately verbatim from the backtester's own implementation
    (`backtest_mr.py: rsi()`), which is what the mean-reversion sleeve's edge was
    measured with. A live rule that computes RSI even slightly differently from
    the backtested one is a DIFFERENT rule with unknown expectancy — the classic
    way a backtest looks good and the bot then loses. If either copy changes, the
    other must change with it (tests/test_strategies.py pins them together).
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + gains[i - 1]) / period
        al = (al * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out
