"""Market-context reads: per-symbol volatility/structure, market regime+breadth,
and earnings-date awareness — for the bots' entry/stop decisions and LLM prompt.

WHY THIS EXISTS
---------------
Every entry and every stop the bots place today comes from 30 daily closes per
symbol (`yolo_run.run_cycle` builds exactly that: last, 5d%, 30d%, 30d high/low).
That is enough arithmetic to place a bracket, and not enough to know whether a
bracket should be placed at all. Four blind spots it cannot see:

  * NO VOLATILITY UNIT. A 1.5x-ATR stop on a 1.8%-ATR name and the same stop on a
    6%-ATR name are the same number to the strategy and very different risk.
    `atr_pct` is the unit the parent should size stops with.
  * NO STRUCTURE. 30 closes cannot say whether 674 is a fresh 20-day high or mid
    range, whether price is above its 200d SMA or extended 25% above it, or
    whether the 50d SMA is rolling over. `range_pos_pct` / `dist_sma*_pct` /
    `sma*_rising` do.
  * NO BACKDROP. The breakout/momentum strategies have been running through chop
    and getting stopped on noise. `regime()` makes that state EXPLICIT and
    auditable (documented label rules below) so the parent can gate on it, and
    `breadth_pct` separates a market move from a single-name move.
  * NO EVENT TAIL. A GTC bracket does not protect an earnings gap: SLV gapped
    through its own stop for -1.74R instead of -1.0R. `earnings_within()` is the
    one unhedged tail risk in the design, made visible before entry.

DESIGN CONTRACT
---------------
* READ-ONLY. This module never places, modifies or cancels an order and imports
  no order path.
* Stdlib only (urllib / json / datetime / zoneinfo / statistics / math), so it
  runs in the container with no pip installs — same constraint as alpaca_rest.py.
* DETERMINISTIC. Every number is arithmetic over bar lists. No LLM calls, no
  randomness.
* NEVER RAISES. The bots call this inside their decision cycle; a data-host blip
  (see the 2026-09-11 /v2/clock HTTP 500 outage) must degrade a FIELD to None,
  never kill the cycle. Anything unavailable is None — never invented, because a
  fabricated 0.0 ATR% would silently size a stop at the live price.
* Indicators run over COMPLETED daily sessions (today's in-progress daily bar is
  excluded), so the structural numbers do not wobble between 09:31 and 16:00 —
  only `last`, the intraday fields and the volume ratio move intraday. Only the
  live price enters the *distances*, which is what a trader actually reads.

CACHING
-------
Module-level TTL cache (bots call this every 5-30 min per symbol; the parent
calls `context_for_llm()` once per cycle over ~19-27 symbols, and `regime()`
alone costs 18 read-only data requests: SPY + 11 sector ETFs + VIXY + QQQ/TLT/
IWM + CBOE). Snapshots 300s, regime 900s, earnings
6h (per-calendar-day payload cached separately so a 10-day window costs 8
requests at most once per 6h). Cached objects are deep-copied on read: callers
annotate/trim these dicts, and a parent that popped a key must not be able to
corrupt another caller's view.
"""
from __future__ import annotations

import copy
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import indicators

ET = ZoneInfo("America/New_York")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
# TTLs (seconds). Chosen against the call cadence, not for freshness: a daily
# ATR%/SMA does not change inside a 5-minute window, and the intraday 5Min
# window is the only genuinely moving input.
SNAPSHOT_TTL_S = 300.0
REGIME_TTL_S = 900.0
EARNINGS_TTL_S = 6 * 3600.0

# 1Day history: 260 sessions covers SMA200 + 10 sessions of slope (~210 needed)
# plus the 30d change and the 20d range. The 1y ATR% percentile in regime() is
# the consumer that actually needs the length: ~250 ATR(14) observations means
# ~264 daily bars, so regime() asks for 300 and takes the tail.
_SNAP_DAILY_BARS = 260
_REGIME_DAILY_BARS = 300
# 200 five-min bars ≈ one full extended session (04:00-20:00 ET = 192 buckets).
# NOTE: this only works when the fetch passes an explicit `start` — see the
# comment at the fetch site.
_INTRADAY_BARS = 200

# Opening range = 09:30-10:00 ET. Publish it only once at least three 5Min bars
# have printed (15 min): a 5-minute "opening range" would be read by the LLM as
# the strategy's 30-minute range and its breakout level would be noise. Before
# that the window is genuinely undefined — None is the honest value.
_ORB_MIN_BARS = 3
_ORB_START, _ORB_END = "09:30", "10:00"
_RTH_START, _RTH_END = "09:30", "16:00"

_SNAP_TEXT_MAX = 300
_REGIME_TEXT_MAX = 200
_CTX_TEXT_MAX = 400

# The 11 SPDR sector ETFs — the standard breadth proxy. ETFs never appear on an
# earnings calendar, which is exactly why earnings_within() returns None for
# them rather than erroring.
SECTOR_ETFS = ("XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
               "XLRE", "XLC")

# VIX primary source: CBOE delayed-quote JSON, no auth (verified 2026-09-17:
# HTTP 200 with data.current_price). It carries only a CURRENT quote — no
# history — so the 5-day change and anything percentile-shaped must come from
# VIXY (the VIX ETF) through client.bars. `vix_source` tells the caller which
# number it is holding.
CBOE_VIX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
_HTTP_TIMEOUT = 20
_RETRY_SLEEP = 2.0
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Nasdaq earnings calendar: no auth, but it 403s a bare urllib request — it
# wants a browser UA (same class of requirement as econ.py's TradingView feed).
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
NASDAQ_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/market-activity/earnings",
}
# Nasdaq's own vocabulary -> ours. Anything else (incl. 'time-not-supplied')
# degrades to 'unknown' rather than a guess: 'before_open' vs 'after_close'
# changes whether the gap risk is tonight or tomorrow morning.
_EARN_TIME = {"time-pre-market": "before_open", "time-after-hours": "after_close"}

SNAP_KEYS = (
    "symbol", "last", "last_close", "chg_1d_pct", "chg_5d_pct", "chg_30d_pct",
    "high_20d", "low_20d", "range_pos_pct",
    "atr14", "atr_pct",
    "rsi14", "sma20", "sma50", "sma200",
    "dist_sma20_pct", "dist_sma50_pct", "dist_sma200_pct",
    "sma50_rising", "sma200_rising",
    "vol_ratio", "gap_pct",
    "intraday_high", "intraday_low", "orb_high", "orb_low",
    "text",
)

REGIME_KEYS = (
    "label", "vix", "vix_source", "vix_chg_5d_pct",
    "spy_last", "spy_sma20", "spy_sma50", "spy_sma200", "spy_trend",
    "spy_atr_pct", "spy_atr_pctile_1y",
    "breadth_pct", "breadth_above_200_pct", "sectors",
    "qqq_chg_5d_pct", "tlt_chg_5d_pct", "iwm_chg_5d_pct",
    "text",
)


# --------------------------------------------------------------------------- #
# TTL cache
# --------------------------------------------------------------------------- #
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_MAX_KEYS = 512  # bound memory: a long-running monitor_service process


def _warn(msg: str) -> None:
    """One-line warning. Never raises, never formats a secret."""
    try:
        print(f"market_ctx: {msg}")
    except Exception:
        pass


def _cache_get(key: str) -> Any:
    """Cached value, or None when missing/expired. Returns a DEEP COPY.

    The caller-visible dicts are handed to prompts and to decision code that
    trims/annotates them; without the copy a `del d['sectors']` in
    context_for_llm() would punch a hole in the shared regime object.
    """
    ent = _CACHE.get(key)
    if ent is None:
        return None
    expires, value = ent
    if time.monotonic() >= expires:
        _CACHE.pop(key, None)
        return None
    return copy.deepcopy(value)


def _cache_put(key: str, value, ttl: float) -> None:
    if len(_CACHE) >= _CACHE_MAX_KEYS:
        # Cheap eviction: drop everything already expired, else the oldest half.
        now = time.monotonic()
        for k in [k for k, (exp, _) in _CACHE.items() if exp <= now]:
            _CACHE.pop(k, None)
        if len(_CACHE) >= _CACHE_MAX_KEYS:
            for k in list(_CACHE)[: _CACHE_MAX_KEYS // 2]:
                _CACHE.pop(k, None)
    _CACHE[key] = (time.monotonic() + ttl, value)


def clear_cache() -> None:
    """Drop every cached read. Ops/tests only — never needed in a bot cycle."""
    _CACHE.clear()


# --------------------------------------------------------------------------- #
# Small helpers (all None-safe; nothing here raises)
# --------------------------------------------------------------------------- #
def _num(v) -> float | None:
    """Coerce a feed value to a finite float, else None (never 0.0).

    A missing/blank field must stay absent: defaulting to 0.0 would make a bar
    with no volume look like a real 0-volume print inside the ratio math.
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _r(v, nd: int = 2) -> float | None:
    """Round for output, or None. Keeps the dict/JSON printable and stable."""
    f = _num(v)
    return None if f is None else round(f, nd)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _bar_et(b) -> datetime | None:
    """Alpaca bar/quote timestamp (ISO8601, e.g. ...Z) -> aware ET datetime."""
    ts = b.get("t") if isinstance(b, dict) else None
    if not isinstance(ts, str) or not ts:
        return None
    s = ts[:-1] + "+00:00" if ts.endswith(("Z", "z")) else ts
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(ET)
    except (OverflowError, OSError, ValueError):
        return None


def _bar_list(payload) -> list[dict]:
    """`client.bars()` payload -> usable bar dicts (the free IEX tier hands back
    `bars: null` for an empty window, and a symbol that does not exist 404s)."""
    if isinstance(payload, dict):
        rows = payload.get("bars") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [b for b in rows if isinstance(b, dict) and _num(b.get("c")) is not None]


def _bars(client, sym: str, timeframe: str, limit: int,
          start: str | None = None) -> list[dict]:
    """Read-only bars fetch. Any failure -> [] (never raises into a bot cycle)."""
    try:
        return _bar_list(client.bars(sym, timeframe=timeframe, limit=limit, start=start))
    except Exception as e:
        _warn(f"bars {sym} {timeframe} failed: {type(e).__name__}: {e}")
        return []


def _latest(client, sym: str) -> float | None:
    """Live last-trade price, or None. Preferred price: Alpaca validates bracket
    legs against the LIVE print, not the daily close (MSFT 2026-09-08: close
    510.12 vs live 490.56 -> stop rejected)."""
    try:
        lt = client.latest_trade(sym)
    except Exception:
        return None
    px = _num((lt or {}).get("price")) if isinstance(lt, dict) else None
    return px if px and px > 0 else None


def _http_get(url: str, headers: dict, timeout: int = _HTTP_TIMEOUT) -> bytes:
    """GET -> bytes, retrying ONCE after ~2s on 429/5xx/transport errors.

    Same policy as econ.py: other 4xx are not retried (a hard block or a bad
    request will not fix itself, and the 2s sleep only burns cycle budget).
    """
    last: Exception | None = None
    for attempt in range(2):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt == 0:
            time.sleep(_RETRY_SLEEP)
    raise last if last is not None else RuntimeError(f"GET {url} failed")


def _http_json(url: str, headers: dict, timeout: int = _HTTP_TIMEOUT):
    return json.loads(_http_get(url, headers, timeout=timeout).decode("utf-8", errors="replace"))


def _split_sessions(daily: list[dict], today: date) -> tuple[list[dict], list[dict], bool]:
    """Split daily bars into (all, completed, last_is_today).

    Alpaca returns TODAY'S in-progress daily bar during the session. Including
    it in the indicator series would make ATR collapse to whatever range has
    printed so far at 09:31, and make "dist from the 20d SMA" jump around on a
    value that is really the live price. So indicators use `completed`; today's
    partial bar is used only where a live value is genuinely wanted (the 20d
    range extremes, today's volume, the fallback intraday high/low).
    """
    if not daily:
        return [], [], False
    partial = (_bar_et(daily[-1]) or datetime.now(ET)).date() == today
    completed = daily[:-1] if partial else daily
    if not completed:      # brand-new listing with a single (today's) bar
        completed, partial = daily, False
    return daily, completed, partial


def _pct_back(closes: list[float], last: float, back: int) -> float | None:
    """% change of `last` from `back` sessions ago, or None when history is short."""
    if last is None or len(closes) <= back:
        return None
    base = closes[-1 - back]
    if not base:
        return None
    return (last / base - 1.0) * 100.0


def _sma_value(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return indicators.sma(closes, period)[-1]


def _sma_rising(closes: list[float], period: int, lookback: int = 10) -> bool | None:
    """Is this SMA higher than it was `lookback` closes ago? None if unknowable.

    Computed on completed closes, so it is a daily-resolution fact (yesterday's
    SMA vs 10 sessions earlier) rather than an intraday wobble.
    """
    if len(closes) < period + lookback:
        return None
    series = indicators.sma(closes, period)
    now, then = series[-1], series[-1 - lookback]
    if now is None or then is None:
        return None
    return now > then


def _rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI over completed closes. None until `period` deltas exist.

    Wilder smoothing (not a plain rolling mean): the first value is the simple
    average of the first `period` gains/losses, then
    avg = (avg * (period-1) + current) / period. That is what every chart shows
    as RSI14 — a rolling-mean variant reads several points different and would
    make 'RSI 68' disagree with the user's own screen.
    """
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = d if d > 0 else 0.0
        loss = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr_pct_series(bars: list[dict]) -> list[float]:
    """ATR(14) as % of that session's close, one value per warm session."""
    if len(bars) < 15:
        return []
    atr_series = indicators.atr(bars, 14)
    out: list[float] = []
    for b, a in zip(bars, atr_series):
        close = _num(b.get("c"))
        if a is not None and close:
            out.append(a / close * 100.0)
    return out


def _pctile_rank(series: list[float], x: float) -> float | None:
    """Percentile (0-100) of x within series, inclusive: share of values <= x.

    Inclusive because the current observation is normally itself in the window;
    excluding it would make a fresh 1-year high read 99.6 instead of 100.
    """
    if not series:
        return None
    n = sum(1 for v in series if v <= x)
    return n / len(series) * 100.0


def _truncate(text: str, limit: int) -> str:
    """Cut at a ' · ' boundary and mark it, so a prompt never gets half a clause."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " · " in cut:
        cut = cut[: cut.rfind(" · ")]
    return cut.rstrip(" ·,;") + "…"


# --------------------------------------------------------------------------- #
# Per-symbol snapshot
# --------------------------------------------------------------------------- #
def _empty_snapshot(sym: str) -> dict:
    """Every key present, every value None — the shape callers can rely on."""
    snap: dict = {k: None for k in SNAP_KEYS}
    snap["symbol"] = sym
    snap["text"] = f"{sym} · no data"
    return snap


def _build_snapshot(client, sym: str) -> dict:
    snap = _empty_snapshot(sym)
    daily = _bars(client, sym, "1Day", _SNAP_DAILY_BARS)
    if not daily:
        return snap

    today = datetime.now(ET).date()
    daily, completed, partial = _split_sessions(daily, today)
    closes = [float(b["c"]) for b in completed]
    # Change-% reference series INCLUDES today's in-progress bar: "5d %" means
    # live vs the close 5 SESSIONS ago, and with today's bar present that close
    # is daily[-6] (today is session 0) — the same number a screener shows and
    # the same span yolo_run already used (closes[-1]/closes[-6]). Running this
    # off the completed series instead counted today as session 1 and reported
    # the 6-session move (verified 2026-09-17: SPY printed -0.01% where the
    # real 5-session move was +0.59%).
    all_closes = [float(b["c"]) for b in daily]
    last_close = closes[-1]

    last = _latest(client, sym) or last_close
    snap["last"] = _r(last)
    snap["last_close"] = _r(last_close)
    snap["chg_1d_pct"] = _r((last / last_close - 1.0) * 100.0 if last_close else None)
    snap["chg_5d_pct"] = _r(_pct_back(all_closes, last, 5))
    # 30 sessions back; None rather than silently reporting a shorter horizon as
    # "30d" (a 260-bar fetch makes it available for anything but a fresh listing).
    snap["chg_30d_pct"] = _r(_pct_back(all_closes, last, 30))

    # 20-day range INCLUDES today's partial bar: on a breakout day the day's own
    # high IS the 20d high, and range_pos_pct must be able to read 100.
    window = daily[-20:]
    highs = [_num(b.get("h")) for b in window]
    lows = [_num(b.get("l")) for b in window]
    highs = [h for h in highs if h is not None]
    lows = [l for l in lows if l is not None]
    high20 = max(highs) if highs else None
    low20 = min(lows) if lows else None
    snap["high_20d"] = _r(high20)
    snap["low_20d"] = _r(low20)
    if high20 is not None and low20 is not None and high20 > low20:
        snap["range_pos_pct"] = _r(_clamp((last - low20) / (high20 - low20) * 100.0, 0.0, 100.0), 1)

    atr_series = indicators.atr(completed, 14)
    atr14 = atr_series[-1] if atr_series else None
    snap["atr14"] = _r(atr14)
    snap["atr_pct"] = _r(atr14 / last * 100.0 if atr14 and last else None)
    snap["rsi14"] = _r(_rsi_wilder(closes, 14), 1)

    for period in (20, 50, 200):
        sma = _sma_value(closes, period)
        snap[f"sma{period}"] = _r(sma)
        snap[f"dist_sma{period}_pct"] = _r((last / sma - 1.0) * 100.0 if sma else None)
    snap["sma50_rising"] = _sma_rising(closes, 50)
    snap["sma200_rising"] = _sma_rising(closes, 200)

    # Relative volume: today's running volume vs the 20 sessions BEFORE it. Early
    # in the session this is inherently understated (it is a partial day); the
    # alternative — extrapolating — would be inventing a number. Outside the
    # session the reference falls back to the last completed bar, so the ratio
    # still means "yesterday's volume vs its own 20d average".
    ref_idx = len(daily) - 1 if partial else len(completed) - 1
    ref_vol = _num(daily[ref_idx].get("v"))
    if not ref_vol:  # today's bar exists but has no volume yet (pre-market)
        ref_vol = _num(completed[-1].get("v")) if completed else None
        ref_idx = len(completed) - 1 if completed else -1
    prior = [_num(b.get("v")) for b in daily[:ref_idx]][-20:]
    prior = [v for v in prior if v]
    if ref_vol and len(prior) >= 5:
        snap["vol_ratio"] = _r(ref_vol / statistics.fmean(prior))

    # Today's 5Min bars: gap, intraday extremes, opening range.
    # The EXPLICIT `start` is load-bearing. alpaca_rest.bars() back-computes a
    # window from `limit` and truncates the response to the FIRST `req_limit`
    # bars, using a bars-per-day table that assumes RTH-only (78 for 5Min) while
    # the data host returns extended-hours buckets too (~192/session). A
    # limit-only 5Min fetch therefore silently returns bars ending DAYS ago
    # (verified from the container 2026-09-17 12:03 ET: last 5Min bar was
    # 2026-09-15T11:00:00Z, so today's gap/ORB/intraday read as absent).
    # Passing `start` bypasses that truncation path entirely.
    five = _bars(client, sym, "5Min", _INTRADAY_BARS, start=today.isoformat())
    sess = [b for b in five if (_bar_et(b) or datetime.now(ET)).date() == today]
    rth = [b for b in sess if _RTH_START <= (_bar_et(b) or datetime.now(ET)).strftime("%H:%M") < _RTH_END]

    today_open = None
    if partial:
        today_open = _num(daily[-1].get("o"))
    if today_open is None and rth:
        today_open = _num(rth[0].get("o"))
    if today_open and last_close:
        snap["gap_pct"] = _r((today_open / last_close - 1.0) * 100.0)

    if rth:
        snap["intraday_high"] = _r(max(_num(b.get("h")) or 0.0 for b in rth))
        snap["intraday_low"] = _r(min(_num(b.get("l")) or 0.0 for b in rth))
        orb = [b for b in rth if _ORB_START <= (_bar_et(b) or datetime.now(ET)).strftime("%H:%M") < _ORB_END]
        if len(orb) >= _ORB_MIN_BARS:
            snap["orb_high"] = _r(max(_num(b.get("h")) or 0.0 for b in orb))
            snap["orb_low"] = _r(min(_num(b.get("l")) or 0.0 for b in orb))
    elif partial:
        # 5Min feed gap during the session — the daily bar's own extremes are the
        # same measurement; better than reporting None for a session in progress.
        snap["intraday_high"] = _r(_num(daily[-1].get("h")))
        snap["intraday_low"] = _r(_num(daily[-1].get("l")))

    snap["text"] = _human_snapshot(snap)
    return snap


def _human_snapshot(s: dict) -> str:
    """One-line read for the LLM, e.g.
    'META 674.00 (+0.8% 1d, +4.7% 5d, +14.3% 30d) · ATR 2.1% · RSI 68 ·
     3.1% above 20d SMA, 12% above 200d SMA · range 82% · vol 1.4x'
    Missing fields are omitted, not filled with placeholders.
    """
    sym = s.get("symbol") or "?"
    if s.get("last") is None:
        return f"{sym} · no data"
    parts: list[str] = []
    head = f"{sym} {s['last']:.2f}"
    chg = [f"{s[k]:+.1f}% {lbl}" for k, lbl in
           (("chg_1d_pct", "1d"), ("chg_5d_pct", "5d"), ("chg_30d_pct", "30d"))
           if s.get(k) is not None]
    if chg:
        head += f" ({', '.join(chg)})"
    parts.append(head)
    if s.get("atr_pct") is not None:
        parts.append(f"ATR {s['atr_pct']:.1f}%")
    if s.get("rsi14") is not None:
        parts.append(f"RSI {s['rsi14']:.0f}")
    dists = []
    for period in (20, 50, 200):
        d = s.get(f"dist_sma{period}_pct")
        if d is None:
            continue
        dists.append(f"{abs(d):.1f}% {'above' if d >= 0 else 'below'} {period}d SMA")
    if dists:
        parts.append(", ".join(dists))
    if s.get("range_pos_pct") is not None:
        parts.append(f"range {s['range_pos_pct']:.0f}%")
    if s.get("vol_ratio") is not None:
        parts.append(f"vol {s['vol_ratio']:.1f}x")
    if s.get("gap_pct") is not None:
        parts.append(f"gap {s['gap_pct']:+.1f}%")
    if s.get("orb_high") is not None:
        parts.append(f"ORB {s['orb_low']:.2f}/{s['orb_high']:.2f}")
    slopes = [f"{p}d{'↑' if s[f'sma{p}_rising'] else '↓'}"
              for p in (50, 200) if s.get(f"sma{p}_rising") is not None]
    if slopes:
        parts.append(" ".join(slopes))
    return _truncate(" · ".join(parts), _SNAP_TEXT_MAX)


def symbol_snapshot(client, sym: str) -> dict:
    """One liquid symbol's full technical read (cached 300s).

    Fetches <=260 1Day bars + today's 5Min bars and returns EXACTLY SNAP_KEYS.
    Anything unavailable is None; a failure returns the same key set all-None
    (with `text` saying so) rather than raising into the caller's cycle.
    """
    sym = str(sym or "").strip().upper()
    if not sym:
        return _empty_snapshot("?")
    key = f"snap:{sym}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        snap = _build_snapshot(client, sym)
    except Exception as e:
        _warn(f"snapshot {sym} failed: {type(e).__name__}: {e}")
        snap = _empty_snapshot(sym)
    # Failures are cached too: a broken symbol must not cost the whole cycle's
    # budget on every 5-minute tick; it recovers on the next TTL expiry.
    _cache_put(key, snap, SNAPSHOT_TTL_S)
    return snap


# --------------------------------------------------------------------------- #
# Regime + breadth
# --------------------------------------------------------------------------- #
def _empty_regime(label: str = "chop") -> dict:
    reg: dict = {k: None for k in REGIME_KEYS}
    reg["label"] = label
    reg["sectors"] = {}
    reg["text"] = "CHOP (regime data unavailable)"
    return reg


def _vix(client) -> tuple[float | None, str | None, float | None]:
    """(vix, source, 5-session % change) from the best available source.

    CBOE gives the real index (authoritative) but ONLY a current quote, so the
    5-day change always comes from VIXY — the VIX ETF — via client.bars, which
    is a proxy: VIXY tracks VIX futures, it is not the index, and it bleeds to
    contango decay, so its change is a direction reading, not a level match.
    """
    chg5: float | None = None
    vixy_closes: list[float] = []
    try:
        bars = _bar_list(_bars(client, "VIXY", "1Day", _REGIME_DAILY_BARS))
        if bars:
            today = datetime.now(ET).date()
            _, completed, _ = _split_sessions(bars, today)
            vixy_closes = [float(b["c"]) for b in completed]
            if len(vixy_closes) >= 6:
                base = vixy_closes[-6]
                if base:
                    chg5 = (vixy_closes[-1] / base - 1.0) * 100.0
    except Exception as e:
        _warn(f"VIXY proxy failed: {type(e).__name__}: {e}")

    try:
        data = _http_json(CBOE_VIX_URL, {"User-Agent": _BROWSER_UA})
        cur = _num(((data or {}).get("data") or {}).get("current_price"))
        if cur and cur > 0:
            return cur, "cboe", _r(chg5)
    except Exception as e:
        _warn(f"CBOE VIX failed: {type(e).__name__}: {e}")

    if vixy_closes:
        return vixy_closes[-1], "vixy", _r(chg5)
    return None, None, _r(chg5)


def _sector_read(client, sym: str) -> dict | None:
    """5d/30d change + above-20d/200d-SMA flags for one sector ETF (daily closes).

    Deliberately close-based, not live-print-based: breadth is a daily concept
    and 11 extra live-trade calls per 15-minute regime refresh buy nothing. The
    regime's SPY 5d baseline (used for rs_5d_vs_spy) is computed the same
    close-based way, so the relative-strength subtraction compares like with
    like — note this is a DIFFERENT convention from symbol_snapshot's
    live-price chg_5d_pct, which is the screener number.
    """
    bars = _bar_list(_bars(client, sym, "1Day", _REGIME_DAILY_BARS))
    if not bars:
        return None
    today = datetime.now(ET).date()
    _, completed, _ = _split_sessions(bars, today)
    closes = [float(b["c"]) for b in completed]
    if len(closes) < 21:
        return None
    last = closes[-1]
    out: dict = {
        "chg_5d_pct": _r(_pct_back(closes, last, 5)),
        "chg_30d_pct": _r(_pct_back(closes, last, 30)),
    }
    out["_sma20"] = _sma_value(closes, 20)
    out["_sma200"] = _sma_value(closes, 200)
    out["_last"] = last
    return out


def _build_regime(client) -> dict:
    reg = _empty_regime()
    # SPY 50d SMA slope: it is a label input (trend_up requires a RISING 50d,
    # trend_down a FALLING one), computed from the same close series as the SMA
    # itself so the label can never disagree with the displayed numbers.
    spy_sma50_rising: bool | None = None

    spy = _bar_list(_bars(client, "SPY", "1Day", _REGIME_DAILY_BARS))
    spy_closes: list[float] = []
    if spy:
        today = datetime.now(ET).date()
        spy_bars, completed, _ = _split_sessions(spy, today)  # noqa: F841 (kept for symmetry)
        spy_closes = [float(b["c"]) for b in completed]
        spy_last = _latest(client, "SPY") or spy_closes[-1]
        sma20 = _sma_value(spy_closes, 20)
        sma50 = _sma_value(spy_closes, 50)
        sma200 = _sma_value(spy_closes, 200)
        reg["spy_last"] = _r(spy_last)
        reg["spy_sma20"], reg["spy_sma50"], reg["spy_sma200"] = _r(sma20), _r(sma50), _r(sma200)
        # Unknown SMAs must not read as a trend: None -> 'mixed'.
        if sma20 and sma50:
            if spy_last > sma20 and spy_last > sma50:
                reg["spy_trend"] = "above_20_50"
            elif spy_last < sma20 and spy_last < sma50:
                reg["spy_trend"] = "below_20_50"
            else:
                reg["spy_trend"] = "mixed"
        else:
            reg["spy_trend"] = "mixed"

        atr_series = indicators.atr(completed, 14)
        atr14 = atr_series[-1] if atr_series else None
        reg["spy_atr_pct"] = _r(atr14 / spy_last * 100.0 if atr14 and spy_last else None)
        hist = _atr_pct_series(completed)[-250:]
        if hist and reg["spy_atr_pct"] is not None:
            reg["spy_atr_pctile_1y"] = _r(_pctile_rank(hist, reg["spy_atr_pct"]), 1)
        spy_sma50_rising = _sma_rising(spy_closes, 50)

    spy_chg5 = _pct_back(spy_closes, spy_closes[-1], 5) if spy_closes else None

    sectors: dict[str, dict] = {}
    above20 = above200 = counted = 0
    for sym in SECTOR_ETFS:
        read = None
        try:
            read = _sector_read(client, sym)
        except Exception as e:
            _warn(f"sector {sym} failed: {type(e).__name__}: {e}")
        if read is None:
            continue
        sma20, sma200, last = read.pop("_sma20"), read.pop("_sma200"), read.pop("_last")
        if sma20 and last > sma20:
            above20 += 1
        if sma200 and last > sma200:
            above200 += 1
        counted += 1
        read["rs_5d_vs_spy"] = _r(read["chg_5d_pct"] - spy_chg5
                                  if read["chg_5d_pct"] is not None and spy_chg5 is not None
                                  else None)
        sectors[sym] = read
    reg["sectors"] = sectors
    # Breadth is over sectors with usable data (a feed gap on one ETF must not
    # silently read as bearish breadth).
    if counted:
        reg["breadth_pct"] = _r(above20 / counted * 100.0, 1)
        reg["breadth_above_200_pct"] = _r(above200 / counted * 100.0, 1)

    for sym, key in (("QQQ", "qqq_chg_5d_pct"), ("TLT", "tlt_chg_5d_pct"),
                     ("IWM", "iwm_chg_5d_pct")):
        try:
            bars = _bar_list(_bars(client, sym, "1Day", 30))
            if bars:
                _, completed, _ = _split_sessions(bars, datetime.now(ET).date())
                closes = [float(b["c"]) for b in completed]
                reg[key] = _r(_pct_back(closes, closes[-1], 5))
        except Exception as e:
            _warn(f"{sym} failed: {type(e).__name__}: {e}")

    vix, source, vix_chg5 = _vix(client)
    reg["vix"], reg["vix_source"], reg["vix_chg_5d_pct"] = _r(vix), source, vix_chg5

    reg["label"] = _label(reg, spy_sma50_rising)
    reg["text"] = _human_regime(reg)
    return reg


def _label(reg: dict, spy_sma50_rising: bool | None = None) -> str:
    """Regime label. The rules are intentionally simple and auditable:

      'vol_spike'  VIX >= 28  OR  VIX rose >= 25% over 5 sessions. Checked FIRST:
                   a spike overrides structure — the bots' stops get swept in
                   that state regardless of which side of the 50d SPY sits.
      'trend_up'   SPY above BOTH its 20d and 50d SMA AND the 50d SMA is rising.
      'trend_down' SPY below BOTH and the 50d SMA is falling.
      'chop'       everything else. This is the one that matters: the
                   breakout/momentum strategies have been running through
                   exactly this regime and getting stopped out on noise.

    `spy_sma50_rising` is passed in (not read back off `reg`) because the slope
    is derived from SPY's close series, not from a key of the output dict.

    With no VIX data the spike rule is inert (it cannot fire on a guessed
    number) and the label falls through to the SPY structure rules; with no SPY
    data at all the answer is 'chop' — the conservative reading, never a
    confident 'trend_up' off missing inputs.
    """
    vix, chg = reg.get("vix"), reg.get("vix_chg_5d_pct")
    if (vix is not None and vix >= 28.0) or (chg is not None and chg >= 25.0):
        return "vol_spike"

    last, s20, s50 = reg.get("spy_last"), reg.get("spy_sma20"), reg.get("spy_sma50")
    if last is None or s20 is None or s50 is None:
        return "chop"
    s50_rising = spy_sma50_rising
    if last > s20 and last > s50 and s50_rising:
        return "trend_up"
    if last < s20 and last < s50 and s50_rising is False:
        return "trend_down"
    return "chop"


def _human_regime(reg: dict) -> str:
    """<=200-char one-liner, e.g.
    'CHOP · SPY 761.29 below 20d (0.9%), above 200d · breadth 45% · VIX 15.7
     (-5.7% 5d) · ATR%ile 22'"""
    parts = [str(reg.get("label") or "chop").upper()]
    last, s20, s200 = reg.get("spy_last"), reg.get("spy_sma20"), reg.get("spy_sma200")
    if last is not None:
        seg = f"SPY {last:.2f}"
        if s20:
            seg += (f" {'above' if last >= s20 else 'below'} 20d "
                    f"({abs((last / s20 - 1) * 100):.1f}%)")
        if s200:
            seg += f", {'above' if last >= s200 else 'below'} 200d"
        parts.append(seg)
    if reg.get("breadth_pct") is not None:
        parts.append(f"breadth {reg['breadth_pct']:.0f}%")
    if reg.get("vix") is not None:
        vix_seg = f"VIX {reg['vix']:.1f}"
        if reg.get("vix_chg_5d_pct") is not None:
            vix_seg += f" ({reg['vix_chg_5d_pct']:+.1f}% 5d)"
        parts.append(vix_seg)
    if reg.get("spy_atr_pctile_1y") is not None:
        parts.append(f"ATR%ile {reg['spy_atr_pctile_1y']:.0f}")
    # Sector detail is NOT inlined here: 11 sectors of RS numbers would push
    # ATR%ile/the VIX read past the 200-char budget on truncation, and the
    # caller that wants it has the full `sectors` dict.
    return _truncate(" · ".join(parts), _REGIME_TEXT_MAX)


def regime(client) -> dict:
    """The market backdrop: label, VIX, SPY structure, breadth, sector RS (cached 900s).

    Returns EXACTLY REGIME_KEYS. Any failure degrades to the chop/None shape
    (plus a warning line) — a data outage must not be able to look like a trend.
    """
    key = "regime"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        reg = _build_regime(client)
    except Exception as e:
        _warn(f"regime failed: {type(e).__name__}: {e}")
        reg = _empty_regime()
    _cache_put(key, reg, REGIME_TTL_S)
    return reg


# --------------------------------------------------------------------------- #
# Earnings
# --------------------------------------------------------------------------- #
def _eps_str(v) -> str | None:
    """Nasdaq epsForecast -> display string, or None for 'no forecast'.

    Nasdaq emits '--' / '' / null for a name without a consensus figure. None
    (not '0') so the LLM sees absence, not a zero estimate.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("--", "-", "N/A", "n/a"):
        return None
    return s


def _fetch_nasdaq_day(day: date) -> list[dict]:
    """One calendar day of the Nasdaq earnings calendar (raises on a bad fetch)."""
    url = NASDAQ_EARNINGS_URL.format(date=day.isoformat())
    data = _http_json(url, NASDAQ_HEADERS)
    if not isinstance(data, dict):
        raise ValueError("nasdaq payload is not an object")
    payload = data.get("data")
    if payload is None:
        # Nasdaq's "nothing scheduled this day" answer is data=None (not an
        # error) — cache it so a holiday in the window costs one request, not
        # one per cycle.
        return []
    rows = (payload or {}).get("rows") if isinstance(payload, dict) else None
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("nasdaq rows is not a list")
    return [r for r in rows if isinstance(r, dict) and str(r.get("symbol") or "").strip()]


def _nasdaq_day(day: date) -> list[dict]:
    """Cached (6h) per-calendar-day Nasdaq rows.

    The per-DAY cache is deliberate: a 10-day look is 8 requests once, then
    free. Failures are NOT cached — a transient Nasdaq 5xx retries on the next
    cycle instead of blanking earnings for 6 hours.
    """
    key = f"earn:{day.isoformat()}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        rows = _fetch_nasdaq_day(day)
    except Exception as e:
        _warn(f"nasdaq {day.isoformat()} failed: {type(e).__name__}: {e}")
        return []
    _cache_put(key, rows, EARNINGS_TTL_S)
    return rows


def earnings_within(client, symbols: list[str], days: int = 5) -> dict[str, dict | None]:
    """{symbol: None | {'date','in_days','time','eps_forecast'}} for the next `days`
    calendar days (today first, weekends skipped).

    WHY: a single-name position held overnight with a GTC bracket is not
    protected against an earnings gap — SLV gapped through its own stop for
    -1.74R instead of -1.0R. An earnings date inside the hold window is the one
    unhedged tail risk in the design.

    Source: Nasdaq's public calendar (no auth, needs a browser User-Agent;
    verified 2026-09-17). Non-equity symbols — ETFs like SPY/GLD/XLE — simply do
    not appear and come back None, which is the correct answer, not an error.
    Fails open to all-None on any error. `client` is unused today (Nasdaq is not
    an Alpaca endpoint) but is kept in the signature for symmetry with the other
    three entry points and for a future Alpaca-sourced fallback.
    """
    syms = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    empty: dict[str, dict | None] = {s: None for s in syms}
    if not syms:
        return {}
    try:
        span = max(1, min(int(days), 30))
        today = datetime.now(ET).date()
        wanted = set(syms)
        found: dict[str, dict | None] = {}
        for i in range(span):
            day = today + timedelta(days=i)
            if day.weekday() >= 5:  # no US equity calendar on a weekend
                continue
            for row in _nasdaq_day(day):
                sym = str(row.get("symbol") or "").strip().upper()
                if sym not in wanted or sym in found:
                    continue
                found[sym] = {
                    "date": day.isoformat(),
                    "in_days": (day - today).days,
                    "time": _EARN_TIME.get(str(row.get("time") or "").strip().lower(),
                                           "unknown"),
                    "eps_forecast": _eps_str(row.get("epsForecast")),
                }
        out = dict(empty)
        out.update(found)
        return out
    except Exception as e:
        _warn(f"earnings_within failed: {type(e).__name__}: {e}")
        return empty


# --------------------------------------------------------------------------- #
# The one call a bot cycle makes
# --------------------------------------------------------------------------- #
def context_for_llm(client, symbols: list[str], with_earnings: bool = True) -> dict:
    """{'regime': <regime minus 'sectors'>, 'symbols': {...}, 'earnings': {...}, 'text': ...}

    Everything is served from the TTL caches, so a 27-symbol cycle costs one
    regime build every 15 minutes and one snapshot per symbol every 5. Degrades
    gracefully: a symbol that fails returns its all-None snapshot (never removed
    silently, so the LLM can see the data is missing rather than the symbol
    absent) and the rest of the dict is unaffected.
    """
    syms = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    try:
        reg_full = regime(client)
    except Exception as e:  # regime() already fail-safes; belt and braces
        _warn(f"context regime failed: {type(e).__name__}: {e}")
        reg_full = _empty_regime()
    reg = {k: v for k, v in reg_full.items() if k != "sectors"}  # keep the call compact

    snaps: dict[str, dict] = {}
    for s in syms:
        try:
            snaps[s] = symbol_snapshot(client, s)
        except Exception as e:  # symbol_snapshot() already fail-safes
            _warn(f"context snapshot {s} failed: {type(e).__name__}: {e}")
            snaps[s] = _empty_snapshot(s)

    earn: dict[str, dict | None] = {}
    if with_earnings and syms:
        try:
            earn = earnings_within(client, syms, 5)
        except Exception as e:
            _warn(f"context earnings failed: {type(e).__name__}: {e}")
            earn = {s: None for s in syms}

    parts = [reg_full.get("text") or ""]
    hits = [f"{s} in {e['in_days']}d ({e['time']})"
            for s, e in earn.items() if e]
    if hits:
        hits.sort(key=lambda t: int(t.split(" in ")[1].split("d")[0]))
        parts.append("earnings<=5d: " + ", ".join(hits[:6]))
    missing = [s for s, d in snaps.items() if d.get("last") is None]
    if missing:
        parts.append("no data: " + ", ".join(missing[:6]))
    text = " | ".join(p for p in parts if p)
    return {
        "regime": reg,
        "symbols": snaps,
        "earnings": earn,
        "text": text[:_CTX_TEXT_MAX],
    }
