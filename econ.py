"""Economic-calendar gate — keep the bots out of high-impact US macro releases.

WHY THIS EXISTS
The deterministic strategies size on ATR/price and the LLM decision layer sees
news + regime, but neither knows that a CPI print lands inside the next 30
minutes. A 1-minute SPY bracket opened at 12:29 UTC on a CPI day is a coin flip
on a 3-sigma candle: the stop is swept by the release spike, not by the setup.
This module gives the bots one cheap, cached question to ask before entry —
"are we inside the pad of a high-impact release?" — plus a compact digest for
the LLM prompt.

Sources (both public, no auth, verified 2026-09-17):
  * PRIMARY   TradingView economic calendar JSON. Carries the real
              importance scale and actual/forecast/previous as numbers.
  * FALLBACK  Forex Factory weekly XML. Used ONLY when TradingView fails.
              Its .json mirror 429s from this host and the forexfactory.com
              HTML page sits behind Cloudflare — do NOT scrape the HTML page.

FAIL OPEN, ALWAYS. This module is advisory. A missing cache, DNS failure, HTTP
429/5xx, a malformed payload, an unparseable date or an empty event list must
all degrade to "no event" — never to an exception escaping into a bot's entry
path, and never to a wrong-but-confident blackout. Every public function is
wrapped: empty results / False / 1.0. The one place we deliberately do NOT fail
open is importance coercion (see `_importance`): garbage importance degrades to
LOW, so a corrupt feed can never fabricate a blackout and freeze trading.

Cache lives at config.DATA_DIR / 'econ_calendar.json' (locally
.../trader/trading/data/, in the container /data/trading/data/). The calendar
window is now-1d .. now+8d, so a 7-day forward look always has data even when
the cache is 6h old.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import config

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
# The calendar changes at most a few times a day; 6h keeps us well inside a
# 7-day forward window while making at most 4 requests/day from the container.
CACHE_TTL_S = 6 * 3600

CACHE_PATH = config.DATA_DIR / "econ_calendar.json"

TV_URL = "https://economic-calendar.tradingview.com/events"
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# TradingView rejects requests without a browser-ish UA + Origin.
TV_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com"}
FF_HEADERS = {"User-Agent": "Mozilla/5.0"}

_HTTP_TIMEOUT = 20
_RETRY_SLEEP = 2.0

# Re-attempt the network at most this often after a failed refresh. Without it a
# dead calendar makes every per-candidate blackout() call pay two timeouts + a
# 2s sleep, which would eat the yolo_run decision cycle budget (300s).
_FAIL_BACKOFF_S = 60.0
_last_fetch_fail = 0.0

ET = ZoneInfo("America/New_York")

# TradingView's real scale: -1 = LOW, 0 = MEDIUM, 1 = HIGH. It is NOT inverted.
_IMP_LABEL = {1: "HIGH", 0: "med", -1: "low"}
# Forex Factory impact strings -> the same scale. Holiday -> LOW (a closed
# market is not a release we blackout on; the parser skips them entirely).
_FF_IMPACT = {"high": 1, "medium": 0, "low": -1, "holiday": -1}
# FF quotes currencies; the bots reason in countries.
_FF_COUNTRY = {"USD": "US"}

_BRIEF_MAX_CHARS = 1200
_MISSING = object()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _warn(msg: str) -> None:
    """One-line warning. Never raises, never formats a secret."""
    try:
        print(f"econ: {msg}")
    except Exception:
        pass


def _parse_ts(ts: object) -> datetime | None:
    """ISO8601 (with or without 'Z' / offset) -> aware UTC datetime, else None."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_utc(now: object = None) -> datetime:
    """Coerce a caller-supplied `now` (None / datetime / ISO8601 string) to UTC.

    Callers must wrap this: an unparseable `now` raises, and every public
    function turns that into its fail-open default.
    """
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        dt = now
    else:
        parsed = _parse_ts(now)
        if parsed is None:
            raise ValueError(f"unparseable now: {now!r}")
        dt = parsed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _num_str(v: object) -> str | None:
    """Raw feed number -> short display string, or None.

    The feeds hand back raw floats (1.31, 4.0, None). '4' reads better than
    '4.0' in an LLM prompt, so strip the trailing '.0'; keep up to 6 decimals
    otherwise. None (and anything non-numeric) stays None — do not invent a
    placeholder, the LLM must see 'no forecast' as absence, not as ''.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        if f.is_integer():
            return str(int(f))
        out = f"{f:.6f}".rstrip("0").rstrip(".")
        return out or "0"
    s = str(v).strip()
    return s or None


def _importance(v: object) -> int:
    """Clamp a raw importance onto the TradingView scale, defaulting to LOW.

    The scale is -1 = LOW, 0 = MEDIUM, 1 = HIGH — do not invert it, and keep
    the -1 tier: `min_importance=-1` must retain low events.
    An unparseable value degrades to -1 (LOW), never to 1: a malformed feed
    must not be able to fabricate a blackout and stall every entry.
    """
    try:
        n = int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1
    return max(-1, min(1, n))


def _make_event(ts: datetime, title: str, country: str, importance: int,
                forecast: object = None, previous: object = None,
                actual: object = None) -> dict:
    """Build the canonical event dict — the exact shape every caller sees."""
    return {
        "ts": _iso_z(ts),
        "date_et": ts.astimezone(ET).strftime("%Y-%m-%d"),
        "title": title,
        "country": country,
        "importance": importance,
        "forecast": _num_str(forecast),
        "previous": _num_str(previous),
        "actual": _num_str(actual),
    }


def _clean_event(raw: object) -> dict | None:
    """Validate/rebuild one event dict from an untrusted source (cache file).

    A hand-edited or truncated cache must not be able to crash or mislead the
    bots, so every field is re-typed here and a bad row is dropped, not trusted.
    """
    if not isinstance(raw, dict):
        return None
    ts = _parse_ts(raw.get("ts"))
    if ts is None:
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    return {
        "ts": _iso_z(ts),
        "date_et": ts.astimezone(ET).strftime("%Y-%m-%d"),
        "title": title,
        "country": str(raw.get("country") or "").strip().upper(),
        "importance": _importance(raw.get("importance")),
        "forecast": _num_str(raw.get("forecast")),
        "previous": _num_str(raw.get("previous")),
        "actual": _num_str(raw.get("actual")),
    }


def _day_of(ev: dict) -> str:
    """ET calendar day 'YYYY-MM-DD' for an event, derived from ts if absent."""
    d = ev.get("date_et")
    if isinstance(d, str) and len(d) == 10:
        return d
    ts = _parse_ts(ev.get("ts"))
    return ts.astimezone(ET).strftime("%Y-%m-%d") if ts else ""


def _et_day_bounds(base: datetime) -> tuple[datetime, datetime]:
    """[00:00 ET, 00:00 ET next day) for the ET calendar day holding `base`.

    Built from wall-clock midnight rather than now±24h: the bots act on the US
    calendar day, and a 05:00 ET call must still see the 02:00 ET release that
    the default now-1h window would have missed.
    """
    d = base.astimezone(ET).date()
    start = datetime(d.year, d.month, d.day, tzinfo=ET)
    return start, start + timedelta(days=1)  # same wall clock next day (DST-safe)


# --------------------------------------------------------------------------- #
# HTTP (stdlib only, one retry, fail-open at the call site)
# --------------------------------------------------------------------------- #
def _http_get(url: str, headers: dict, timeout: int = _HTTP_TIMEOUT) -> bytes:
    """GET returning raw bytes; retries ONCE after ~2s.

    Retryable: HTTP 429 / 5xx and transport errors (DNS, reset, read timeout).
    NOT retryable: other 4xx — a bad request or a hard block won't fix itself
    and the 2s sleep only burns a decision cycle.
    Returns bytes so callers can honour a declared charset (the FF XML is
    windows-1252 and ElementTree decodes the declaration itself).
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


# --------------------------------------------------------------------------- #
# Source 1 — TradingView JSON (primary)
# --------------------------------------------------------------------------- #
def tv_events(data: object) -> list[dict]:
    """Normalize a TradingView `{"status":"ok","result":[...]}` payload.

    Split out from the fetch so the mapping (and the -1/0/1 scale) is testable
    without the network. Raises on a malformed envelope — refresh() catches it
    and falls through to the next source.
    """
    if not isinstance(data, dict) or data.get("status") != "ok":
        status = data.get("status") if isinstance(data, dict) else type(data).__name__
        raise ValueError(f"tradingview status={status}")
    rows = data.get("result") or []
    if not isinstance(rows, list):
        raise ValueError("tradingview result is not a list")
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = _parse_ts(r.get("date"))
        title = str(r.get("title") or "").strip()
        if ts is None or not title:
            continue  # a bad row never aborts the batch
        out.append(_make_event(
            ts, title,
            str(r.get("country") or "").strip().upper(),
            _importance(r.get("importance")),
            r.get("forecast"), r.get("previous"), r.get("actual"),
        ))
    return out


def _fetch_tradingview() -> list[dict]:
    """Upcoming (now-1d .. now+8d) events from the TradingView calendar.

    The window is deliberately wider than the 7 days we serve: it gives slack
    for a stale 6h cache and for the -1d tail so `events()` can still see an
    event that fired an hour ago.
    """
    now = datetime.now(timezone.utc)
    frm = _iso_z(now - timedelta(days=1))
    to = _iso_z(now + timedelta(days=8))
    url = f"{TV_URL}?from={frm}&to={to}&countries=US"
    raw = _http_get(url, TV_HEADERS)
    return tv_events(json.loads(raw.decode("utf-8", errors="replace")))


# --------------------------------------------------------------------------- #
# Source 2 — Forex Factory weekly XML (fallback)
# --------------------------------------------------------------------------- #
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_FF_DATE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")
_FF_TIME = re.compile(r"^(\d{1,2}):(\d{2})\s*(am|pm)$")


def ff_hm(time_s: str) -> tuple[int, int]:
    """FF wall-clock time -> (hour24, minute).

    FF emits bare US-Eastern wall-clock with NO timezone marker ('2:30am').
    An empty <time/> means an all-day item; treat it as 00:00 ET rather than
    dropping the row, so the caller decides (same for 'All Day' / 'Tentative'
    if FF ever emits them as text).
    """
    s = (time_s or "").strip().lower()
    m = _FF_TIME.match(s)
    if not m:
        return 0, 0
    h = int(m.group(1)) % 12          # 12am -> 0, 12pm -> 12
    if m.group(3) == "pm":
        h += 12
    return h, int(m.group(2))


def ff_dt(date_s: str, time_s: str) -> datetime | None:
    """FF date '09-18-2026' + time '2:30am' -> aware ET datetime, else None.

    CRITICAL: these are US-EASTERN (America/New_York) wall-clock values. They
    must go through ZoneInfo so EDT/EST is applied correctly — a 08:30am ET CPI
    print has to come out as 12:30Z in summer and 13:30Z in winter. Treating
    them as UTC would put every release 4-5 hours late and blackout the wrong
    window.
    """
    m = _FF_DATE.match((date_s or "").strip())
    if not m:
        return None
    mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh, mi = ff_hm(time_s)
    try:
        return datetime(yy, mm, dd, hh, mi, tzinfo=ET)
    except ValueError:
        return None


def _xml_text(ev: ElementTree.Element, tag: str) -> str:
    el = ev.find(tag)
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def parse_ff_xml(raw: bytes | str) -> list[dict]:
    """Parse the FF weekly XML into normalized events.

    Accepts bytes (the real response, windows-1252 per its declaration) or a
    str (tests / a cached copy). Holiday rows and unparseable dates/empty
    titles are skipped — a bad row never aborts the batch.
    """
    if isinstance(raw, str):
        # ElementTree refuses a str that still carries an encoding declaration.
        raw = _XML_DECL.sub("", raw, count=1).encode("utf-8")
    root = ElementTree.fromstring(raw)
    out: list[dict] = []
    for ev in root.iter("event"):
        impact = _xml_text(ev, "impact").lower()
        if impact == "holiday":
            continue  # market closure, not a release worth a blackout
        ts = ff_dt(_xml_text(ev, "date"), _xml_text(ev, "time"))
        title = _xml_text(ev, "title")
        if ts is None or not title:
            continue
        ccy = _xml_text(ev, "country").upper()
        out.append(_make_event(
            ts, title,
            _FF_COUNTRY.get(ccy, ccy),
            _FF_IMPACT.get(impact, -1),
            _xml_text(ev, "forecast") or None,
            _xml_text(ev, "previous") or None,
            _xml_text(ev, "actual") or None,
        ))
    return out


def _fetch_forexfactory() -> list[dict]:
    return parse_ff_xml(_http_get(FF_URL, FF_HEADERS))


# Ordered source list; tests monkeypatch this to simulate outages without
# touching the network.
_SOURCES: tuple[tuple[str, object], ...] = (
    ("tradingview", _fetch_tradingview),
    ("forexfactory", _fetch_forexfactory),
)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def _load_cache() -> dict:
    """Read + validate the on-disk cache. Returns {} when absent or unusable."""
    try:
        raw = CACHE_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    rows = data.get("events")
    if not isinstance(rows, list):
        return {}
    events = [e for e in (_clean_event(r) for r in rows) if e is not None]
    return {
        "fetched_at": data.get("fetched_at"),
        "source": str(data.get("source") or "cache"),
        "events": events,
    }


def _cache_age_s(cache: dict) -> float | None:
    fetched = _parse_ts(cache.get("fetched_at"))
    if fetched is None:
        return None
    return (datetime.now(timezone.utc) - fetched).total_seconds()


def _cache_fresh(cache: dict) -> bool:
    age = _cache_age_s(cache)
    # No/invalid fetched_at -> treat as stale so we refresh once and fix it.
    return age is not None and 0 <= age < CACHE_TTL_S


def _write_cache(payload: dict) -> None:
    """Atomic best-effort write — a cache write failure must not break refresh."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        _warn(f"cache write failed: {type(e).__name__}: {e}")


def refresh(force: bool = False) -> dict:
    """Fetch the calendar and (re)write the JSON cache. NEVER raises.

    Returns {'fetched_at': ISO8601Z, 'source': 'tradingview'|'forexfactory'
    |'cache'|'none', 'events': [...]}.

    * force=False and the cache is younger than CACHE_TTL_S -> served as
      'cache' with no network call.
    * otherwise try TradingView, then Forex Factory. A source that raises OR
      returns zero events falls through to the next one (an empty calendar is
      treated as a soft failure — we would rather serve the stale cache than
      cache an empty week).
    * total failure -> source 'none', empty events, one warning line, and the
      OLD cache file is left untouched for the bots to keep using.
    """
    now = datetime.now(timezone.utc)
    try:
        if not force:
            cache = _load_cache()
            if cache and _cache_fresh(cache):
                # Report 'cache' (not the stored source) so a caller can tell
                # an on-disk hit from a fresh network fetch at a glance.
                return {**cache, "source": "cache"}
    except Exception as e:  # pragma: no cover - defensive
        _warn(f"refresh: cache read failed: {type(e).__name__}: {e}")

    for name, fetcher in _SOURCES:
        try:
            events = fetcher()  # type: ignore[operator]
        except Exception as e:
            _warn(f"{name} fetch failed: {type(e).__name__}: {e}")
            continue
        if not events:
            _warn(f"{name} returned no events")
            continue
        events = sorted(events, key=lambda e: e["ts"])
        payload = {"fetched_at": _iso_z(now), "source": name, "events": events}
        _write_cache(payload)
        return payload

    _warn("no calendar source available — trading continues unblocked (fail open)")
    return {"fetched_at": _iso_z(now), "source": "none", "events": []}


def _calendar() -> list[dict]:
    """Events for the bots: fresh cache, else refresh, else the stale cache.

    This is the only place that triggers an automatic network call, so the
    failure backoff lives here: after a failed refresh we reuse whatever the
    disk cache held for _FAIL_BACKOFF_S instead of re-paying two timeouts on
    every per-candidate blackout() check.
    """
    global _last_fetch_fail
    cache = _load_cache()
    if cache and _cache_fresh(cache):
        return cache["events"]
    if time.monotonic() - _last_fetch_fail < _FAIL_BACKOFF_S:
        return cache.get("events") or []  # inside the failure backoff
    result = refresh(force=True)
    if result.get("source") != "none":
        return result.get("events") or []
    _last_fetch_fail = time.monotonic()
    return cache.get("events") or []  # A STALE CACHE STILL GATES TRADES


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def events(from_ts: str | None = None, to_ts: str | None = None,
           min_importance: int = -1,
           countries: tuple[str, ...] | None = None) -> list[dict]:
    """Normalized events in [from_ts, to_ts], sorted ascending by ts.

    Default window is now-1h .. now+7d. min_importance is the TradingView
    scale: -1 keeps everything (INCLUDING low — do not drop the -1 tier),
    0 keeps medium+high, 1 keeps high only. countries=None means every country
    the calendar carries; ('US',) filters to US releases.

    Fail-open: any problem returns [].
    """
    try:
        now = _now_utc()
        lo = _parse_ts(from_ts) if from_ts else None
        hi = _parse_ts(to_ts) if to_ts else None
        lo = lo or (now - timedelta(hours=1))
        hi = hi or (now + timedelta(days=7))
        if hi < lo:
            return []
        floor = _importance(min_importance) if min_importance is not None else -1
        out: list[dict] = []
        for e in _calendar():
            ts = _parse_ts(e.get("ts"))
            if ts is None or ts < lo or ts > hi:
                continue
            imp = _importance(e.get("importance"))
            if imp < floor:
                continue
            if countries is not None and str(e.get("country") or "") not in countries:
                continue
            out.append(e)
        out.sort(key=lambda e: e["ts"])
        return out
    except Exception as e:
        _warn(f"events failed: {type(e).__name__}: {e}")
        return []


def brief(now=None, days: int = 3, countries: tuple[str, ...] = ("US",),
          min_importance: int = 0, max_items: int = 14) -> str:
    """Compact one-string digest for an LLM prompt, grouped by ET calendar day.

    e.g. 'Thu Sep 17: 12:30Z Housing Starts (HIGH), 18:00Z Fed Speak (med);
    Fri Sep 18: 13:15Z Industrial Production (med)'

    High-impact items are listed first inside each day, marked '(HIGH)'. Times
    are HH:MMZ (UTC). Kept under ~1200 chars with a trailing ', …' so it can be
    pasted into a prompt without blowing the budget. Returns '' when silent.
    """
    try:
        base = _now_utc(now)
        rows = events(from_ts=_iso_z(base - timedelta(hours=1)),
                      to_ts=_iso_z(base + timedelta(days=max(0, int(days)))),
                      min_importance=min_importance,
                      countries=countries)
        if not rows:
            return ""
        # Keep the most material items, then re-sort chronologically to display.
        rows = sorted(rows, key=lambda e: (-_importance(e.get("importance")), e["ts"]))
        rows = rows[:max(1, int(max_items))]
        rows.sort(key=lambda e: e["ts"])

        groups: dict[str, list[dict]] = {}
        for e in rows:
            day = _day_of(e)
            if day:
                groups.setdefault(day, []).append(e)

        parts: list[str] = []
        for day in sorted(groups):
            items = sorted(groups[day],
                           key=lambda e: (-_importance(e.get("importance")), e["ts"]))
            body = ", ".join(
                f"{e['ts'][11:16]}Z {e['title']} ({_IMP_LABEL[_importance(e.get('importance'))]})"
                for e in items
            )
            parts.append(f"{_day_label(day)}: {body}")
        out = "; ".join(parts)
        if len(out) > _BRIEF_MAX_CHARS:
            out = out[: _BRIEF_MAX_CHARS - 3].rstrip(" ,;") + ", …"
        return out
    except Exception as e:
        _warn(f"brief failed: {type(e).__name__}: {e}")
        return ""


def _day_label(day: str) -> str:
    """'2026-09-17' -> 'Thu Sep 17'."""
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return day
    return f"{d.strftime('%a %b')} {d.day}"


def blackout(now=None, pad_min: int = 30, min_importance: int = 1,
             countries: tuple[str, ...] = ("US",)) -> tuple[bool, str]:
    """(True, '<title> at HH:MM UTC') when `now` is within ±pad_min of a release.

    Window edges are INCLUSIVE: exactly pad_min away still blacks out, because
    the release spike is not symmetric — the pre-positioning run starts before
    the print. Reports the NEAREST qualifying release (the one this blackout is
    actually about), not merely the first in the window.

    Fail-open: (False, '') on any problem.
    """
    try:
        base = _now_utc(now)
        pad = timedelta(minutes=max(0, int(pad_min)))
        lo, hi = base - pad, base + pad
        rows = events(from_ts=_iso_z(lo), to_ts=_iso_z(hi),
                      min_importance=min_importance, countries=countries)
        if not rows:
            return False, ""
        best, best_delta = rows[0], None
        for e in rows:  # nearest to now — the release this blackout is about
            ts = _parse_ts(e["ts"])
            if ts is None:
                continue
            delta = abs((ts - base).total_seconds())
            if best_delta is None or delta < best_delta:
                best, best_delta = e, delta
        return True, f"{best['title']} at {best['ts'][11:16]} UTC"
    except Exception as e:
        _warn(f"blackout failed: {type(e).__name__}: {e}")
        return False, ""


def event_day(now=None, min_importance: int = 0,
              countries: tuple[str, ...] = ("US",)) -> tuple[bool, str]:
    """(True, '<title> HH:MM UTC') if a qualifying event is on the current ET day.

    The ET day is resolved explicitly (00:00 ET -> 00:00 ET) rather than via the
    default now±window, so a 05:00 ET call still sees a 02:00 ET release that
    the now-1h window would have missed. Reports the most important event of the
    day (earliest wins ties). Fail-open: (False, '').
    """
    try:
        base = _now_utc(now)
        start_et, end_et = _et_day_bounds(base)
        day = start_et.strftime("%Y-%m-%d")
        rows = [e for e in events(from_ts=_iso_z(start_et), to_ts=_iso_z(end_et),
                                  min_importance=min_importance,
                                  countries=countries)
                if _day_of(e) == day]
        if not rows:
            return False, ""
        best = sorted(rows, key=lambda e: (-_importance(e.get("importance")), e["ts"]))[0]
        return True, f"{best['title']} {best['ts'][11:16]} UTC"
    except Exception as e:
        _warn(f"event_day failed: {type(e).__name__}: {e}")
        return False, ""


def high_impact_today(now=None, countries: tuple[str, ...] = ("US",)) -> list[dict]:
    """All importance==1 events on the current ET calendar day.

    Highest importance first, then by time. Fail-open: [].
    """
    try:
        base = _now_utc(now)
        start_et, end_et = _et_day_bounds(base)
        day = start_et.strftime("%Y-%m-%d")
        rows = [e for e in events(from_ts=_iso_z(start_et), to_ts=_iso_z(end_et),
                                  min_importance=1, countries=countries)
                if _day_of(e) == day]
        rows.sort(key=lambda e: (-_importance(e.get("importance")), e["ts"]))
        return rows
    except Exception as e:
        _warn(f"high_impact_today failed: {type(e).__name__}: {e}")
        return []


def size_multiplier(now=None) -> float:
    """Position-size scaler for the bots.

    1.0 normally; 0.5 when a high-impact release lands anywhere on today's ET
    calendar; 0.0 while inside the blackout pad. Fail-open: 1.0 — a calendar
    problem must never shrink or halt trading, only a REAL event may.
    """
    try:
        if blackout(now=now)[0]:
            return 0.0
        if event_day(now=now, min_importance=1)[0]:
            return 0.5
        return 1.0
    except Exception as e:
        _warn(f"size_multiplier failed: {type(e).__name__}: {e}")
        return 1.0
