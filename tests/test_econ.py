"""Plain-assert test suite for econ.py — run with: python3 tests/test_econ.py

No pytest (the repo is stdlib-only, no pip installs). Each check runs in its
own try/except so one failure cannot hide the rest; the process exits non-zero
if anything failed.

    cd /opt/data/profiles/trader/trading
    TRADER_HOME=/opt/data/profiles/trader python3 tests/test_econ.py

Network isolation: every scenario that must be deterministic installs a
synthetic cache file and swaps econ._SOURCES for a fetcher that raises, so the
suite never depends on TradingView/Forex Factory being up. One check
deliberately exercises the REAL transport-failure path against an unresolvable
host (.invalid, RFC 2606) to prove the retry + fail-open behaviour.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The suite lives in tests/; make ../ importable (econ + config live there).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import econ  # noqa: E402

UTC = timezone.utc

_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []


def run(name: str, fn) -> None:
    try:
        fn()
    except AssertionError as e:
        _FAIL.append((name, f"AssertionError: {e}"))
        print(f"FAIL  {name}  <- {e}")
    except Exception as e:  # a crash is a failure too, and we keep going
        _FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"FAIL  {name}  <- unexpected {type(e).__name__}: {e}")
    else:
        _PASS.append(name)
        print(f"ok    {name}")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
_TMP = Path(tempfile.mkdtemp(prefix="econ_tests_"))

# Explicit window covering the pinned fixture dates. Most checks pass a window
# or a `now`, so the suite does not silently depend on the wall clock.
WIN_FROM = "2026-09-17T00:00:00Z"
WIN_TO = "2026-09-18T00:00:00Z"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ev(ts: str, title: str, importance: int, country: str = "US",
        forecast=None, previous=None, actual=None) -> dict:
    dt = econ._parse_ts(ts)
    return {
        "ts": _iso(dt),
        "date_et": dt.astimezone(econ.ET).strftime("%Y-%m-%d"),
        "title": title,
        "country": country,
        "importance": importance,
        "forecast": forecast,
        "previous": previous,
        "actual": actual,
    }


def install_cache(events: list[dict], age_s: float = 0.0, name: str = "c.json") -> Path:
    """Write a synthetic cache and point econ at it."""
    path = _TMP / name
    fetched = datetime.now(UTC) - timedelta(seconds=age_s)
    path.write_text(json.dumps({
        "fetched_at": _iso(fetched),
        "source": "tradingview",
        "events": events,
    }), encoding="utf-8")
    econ.CACHE_PATH = path
    econ._last_fetch_fail = 0.0
    return path


def _boom():
    raise RuntimeError("simulated source outage (offline test mode)")


def go_offline() -> None:
    """Make every calendar source fail without touching the network."""
    econ._SOURCES = (("tradingview", _boom), ("forexfactory", _boom))
    econ._last_fetch_fail = 0.0


def go_live_sources() -> None:
    econ._SOURCES = (
        ("tradingview", econ._fetch_tradingview),
        ("forexfactory", econ._fetch_forexfactory),
    )
    econ._last_fetch_fail = 0.0


def quiet(fn, *a, **kw):
    """Run fn with stdout swallowed — these paths print warnings by design."""
    with redirect_stdout(io.StringIO()) as buf:
        out = fn(*a, **kw)
    return out, buf.getvalue()


# --------------------------------------------------------------------------- #
# 1. importance scale — -1/0/1 preserved, never inverted
# --------------------------------------------------------------------------- #
def test_importance_scale_not_inverted():
    assert econ._importance(1) == 1
    assert econ._importance(0) == 0
    assert econ._importance(-1) == -1, "the LOW tier must survive as -1"
    assert econ._importance("1") == 1
    assert econ._importance(7) == 1, "high values clamp to 1"
    assert econ._importance(-9) == -1, "low values clamp to -1"
    # garbage degrades to LOW, never HIGH — a corrupt feed must not fabricate a blackout
    assert econ._importance(None) == -1
    assert econ._importance("high") == -1
    assert econ._importance(2.7) == 1


def test_tv_payload_keeps_all_three_tiers():
    payload = {
        "status": "ok",
        "result": [
            {"date": "2026-09-18T12:30:00.000Z", "title": "CPI m/m",
             "country": "US", "importance": 1, "forecast": 0.3,
             "previous": 0.2, "actual": None, "currency": "USD"},
            {"date": "2026-09-18T14:00:00.000Z", "title": "Philly Fed",
             "country": "US", "importance": 0, "forecast": 1.31,
             "previous": 1.0, "actual": None, "currency": "USD"},
            {"date": "2026-09-18T18:00:00.000Z", "title": "Fed Speak",
             "country": "US", "importance": -1, "forecast": None,
             "previous": None, "actual": None, "currency": "USD"},
            # junk rows must be dropped, not raised on
            {"date": "not-a-date", "title": "Broken", "importance": 1},
            {"date": "2026-09-18T19:00:00.000Z", "title": "", "importance": 1},
            "not even a dict",
        ],
    }
    evs = econ.tv_events(payload)
    assert len(evs) == 3, f"expected 3 clean rows, got {len(evs)}"
    assert [e["importance"] for e in evs] == [1, 0, -1], "scale was inverted/reordered"
    assert evs[0]["title"] == "CPI m/m"
    assert evs[0]["country"] == "US"
    assert evs[0]["forecast"] == "0.3"
    assert evs[0]["previous"] == "0.2"
    assert evs[0]["actual"] is None
    assert evs[1]["forecast"] == "1.31"
    assert evs[2]["forecast"] is None


def test_tv_number_formatting_strips_trailing_dot_zero():
    assert econ._num_str(4.0) == "4"
    assert econ._num_str(4) == "4"
    assert econ._num_str(1.31) == "1.31"
    assert econ._num_str(3.75) == "3.75"
    assert econ._num_str(1.309) == "1.309"
    assert econ._num_str(None) is None
    assert econ._num_str(float("nan")) is None
    assert econ._num_str(True) is None, "a bool is not a macro number"


def test_tv_bad_envelope_raises_for_refresh_to_catch():
    for bad in ({"status": "error"}, [], "x", None, {"status": "ok", "result": "nope"}):
        try:
            econ.tv_events(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for payload {bad!r}")


# --------------------------------------------------------------------------- #
# 2. Forex Factory XML: ET wall-clock -> UTC (the classic off-by-4h bug)
# --------------------------------------------------------------------------- #
FF_XML = """<?xml version="1.0" encoding="windows-1252"?>
<weeklyevents>
\t<event>
\t\t<title>CPI m/m</title>
\t\t<country>USD</country>
\t\t<date><![CDATA[09-18-2026]]></date>
\t\t<time><![CDATA[2:30am]]></time>
\t\t<impact><![CDATA[High]]></impact>
\t\t<forecast><![CDATA[0.3]]></forecast>
\t\t<previous><![CDATA[0.2]]></previous>
\t</event>
\t<event>
\t\t<title>Non-Farm Employment Change</title>
\t\t<country>USD</country>
\t\t<date><![CDATA[09-18-2026]]></date>
\t\t<time><![CDATA[8:30am]]></time>
\t\t<impact><![CDATA[High]]></impact>
\t\t<forecast />
\t\t<previous><![CDATA[142K]]></previous>
\t</event>
\t<event>
\t\t<title>Bank Holiday</title>
\t\t<country>USD</country>
\t\t<date><![CDATA[09-18-2026]]></date>
\t\t<time><![CDATA[]]></time>
\t\t<impact><![CDATA[Holiday]]></impact>
\t\t<forecast />
\t\t<previous />
\t</event>
\t<event>
\t\t<title>All Day Item</title>
\t\t<country>USD</country>
\t\t<date><![CDATA[09-18-2026]]></date>
\t\t<time><![CDATA[]]></time>
\t\t<impact><![CDATA[Medium]]></impact>
\t\t<forecast />
\t\t<previous />
\t</event>
\t<event>
\t\t<title>German Ifo Business Climate</title>
\t\t<country>EUR</country>
\t\t<date><![CDATA[09-18-2026]]></date>
\t\t<time><![CDATA[4:00am]]></time>
\t\t<impact><![CDATA[Low]]></impact>
\t\t<forecast />
\t\t<previous />
\t</event>
</weeklyevents>
"""


def test_ff_xml_et_to_utc_conversion():
    evs = econ.parse_ff_xml(FF_XML)
    by_title = {e["title"]: e for e in evs}

    assert "Bank Holiday" not in by_title, "Holiday rows must be skipped"

    cpi = by_title["CPI m/m"]
    # 2026-09-18 is EDT (UTC-4): 02:30 ET -> 06:30Z
    assert cpi["ts"] == "2026-09-18T06:30:00Z", f"got {cpi['ts']}"
    assert cpi["date_et"] == "2026-09-18"
    assert cpi["country"] == "US", "USD must normalize to US"
    assert cpi["importance"] == 1
    assert cpi["forecast"] == "0.3"
    assert cpi["previous"] == "0.2"

    nfp = by_title["Non-Farm Employment Change"]
    # THE canonical sanity check: 8:30am ET must read 12:30Z in summer.
    assert nfp["ts"] == "2026-09-18T12:30:00Z", f"got {nfp['ts']}"
    assert nfp["forecast"] is None
    assert nfp["previous"] == "142K", "non-numeric text must pass through"

    allday = by_title["All Day Item"]
    assert allday["ts"] == "2026-09-18T04:00:00Z", "empty <time/> -> 00:00 ET (04:00Z)"

    eur = by_title["German Ifo Business Climate"]
    assert eur["ts"] == "2026-09-18T08:00:00Z"
    assert eur["country"] == "EUR", "non-USD currency passes through unmapped"
    assert eur["importance"] == -1


def test_ff_impact_mapping_table():
    assert econ._FF_IMPACT["high"] == 1
    assert econ._FF_IMPACT["medium"] == 0
    assert econ._FF_IMPACT["low"] == -1
    assert econ._FF_IMPACT["holiday"] == -1


def test_ff_dst_winter_offset():
    """January is EST (UTC-5): 08:30 ET must be 13:30Z, not 12:30Z."""
    winter = econ.ff_dt("01-15-2027", "8:30am")
    assert winter is not None
    assert econ._iso_z(winter) == "2027-01-15T13:30:00Z", econ._iso_z(winter)
    summer = econ.ff_dt("07-15-2026", "8:30am")
    assert econ._iso_z(summer) == "2026-07-15T12:30:00Z", econ._iso_z(summer)


def test_ff_time_parsing_edges():
    assert econ.ff_hm("2:30am") == (2, 30)
    assert econ.ff_hm("12:30am") == (0, 30), "12am is midnight, not noon"
    assert econ.ff_hm("12:00pm") == (12, 0)
    assert econ.ff_hm("8:15PM") == (20, 15), "uppercase pm"
    assert econ.ff_hm("") == (0, 0)
    assert econ.ff_hm("All Day") == (0, 0)
    assert econ.ff_dt("13-45-2026", "8:30am") is None, "bad date -> None, no raise"
    assert econ.ff_dt("", "") is None


def test_ff_xml_malformed_bytes_do_not_escape():
    for bad in (b"", b"<not-xml", b"<weeklyevents><event>", b"\x00\x01"):
        try:
            out = econ.parse_ff_xml(bad)
        except Exception:
            continue  # raising here is fine: refresh() catches it per-source
        assert out == []


# --------------------------------------------------------------------------- #
# 3. min_importance filtering
# --------------------------------------------------------------------------- #
TIERS = [
    _ev("2026-09-17T12:30:00Z", "CPI m/m", 1, forecast="0.3", previous="0.2"),
    _ev("2026-09-17T14:00:00Z", "Philadelphia Fed", 0),
    _ev("2026-09-17T18:00:00Z", "Fed Speak", -1),
]


def test_min_importance_filtering():
    install_cache(TIERS, age_s=0)
    go_offline()  # cache is fresh; nothing should hit the network anyway

    allthree = econ.events(from_ts=WIN_FROM, to_ts=WIN_TO, min_importance=-1)
    assert len(allthree) == 3, f"-1 must keep the LOW tier too, got {len(allthree)}"
    assert [e["importance"] for e in allthree] == [1, 0, -1]

    med_plus = econ.events(from_ts=WIN_FROM, to_ts=WIN_TO, min_importance=0)
    assert len(med_plus) == 2, f"0 keeps medium+high, got {len(med_plus)}"
    assert [e["importance"] for e in med_plus] == [1, 0]

    high_only = econ.events(from_ts=WIN_FROM, to_ts=WIN_TO, min_importance=1)
    assert len(high_only) == 1
    assert high_only[0]["title"] == "CPI m/m"


def test_event_dict_shape_is_exact():
    install_cache(TIERS, age_s=0)
    go_offline()
    e = econ.events(from_ts=WIN_FROM, to_ts=WIN_TO, min_importance=1)[0]
    assert set(e) == {"ts", "date_et", "title", "country", "importance",
                      "forecast", "previous", "actual"}, set(e)
    assert isinstance(e["importance"], int) and not isinstance(e["importance"], bool)
    assert e["ts"] == "2026-09-17T12:30:00Z"
    assert e["date_et"] == "2026-09-17", "12:30Z is 08:30 ET on the same day"


def test_events_window_and_country_filters():
    rows = [
        _ev("2026-09-17T12:30:00Z", "US CPI", 1, country="US"),
        _ev("2026-09-17T12:30:00Z", "German CPI", 1, country="DE"),
        _ev("2026-09-17T12:30:00Z", "UK CPI", 1, country="GB"),
    ]
    install_cache(rows, age_s=0)
    go_offline()

    us = econ.events(from_ts="2026-09-17T00:00:00Z", to_ts="2026-09-18T00:00:00Z",
                     countries=("US",))
    assert [e["country"] for e in us] == ["US"]

    eu = econ.events(from_ts="2026-09-17T00:00:00Z", to_ts="2026-09-18T00:00:00Z",
                     countries=("DE", "GB"))
    assert sorted(e["country"] for e in eu) == ["DE", "GB"]

    every = econ.events(from_ts="2026-09-17T00:00:00Z", to_ts="2026-09-18T00:00:00Z")
    assert len(every) == 3, "countries=None means all"

    none_in_window = econ.events(from_ts="2026-10-01T00:00:00Z",
                                 to_ts="2026-10-02T00:00:00Z")
    assert none_in_window == []

    inverted = econ.events(from_ts="2026-09-18T00:00:00Z", to_ts="2026-09-17T00:00:00Z")
    assert inverted == []


def test_events_sorted_ascending():
    rows = [_ev("2026-09-17T18:00:00Z", "C", -1),
            _ev("2026-09-17T12:30:00Z", "A", 1),
            _ev("2026-09-17T14:00:00Z", "B", 0)]
    install_cache(rows, age_s=0)
    go_offline()
    out = econ.events(from_ts=WIN_FROM, to_ts=WIN_TO, min_importance=-1)
    assert [e["title"] for e in out] == ["A", "B", "C"]


# --------------------------------------------------------------------------- #
# 4. blackout() window edges
# --------------------------------------------------------------------------- #
def test_blackout_window_edges():
    event_ts = "2026-09-17T12:30:00Z"
    install_cache([_ev(event_ts, "CPI m/m", 1)], age_s=0)
    go_offline()

    def at(minutes_from, title="CPI m/m", importance=1):
        install_cache([_ev(event_ts, title, importance)], age_s=0)
        go_offline()
        when = (datetime.fromisoformat("2026-09-17T12:30:00+00:00")
                + timedelta(minutes=minutes_from))
        return econ.blackout(now=when, pad_min=30)

    blocked, why = at(-29)
    assert blocked is True, "29 min before the release must black out"
    assert why == "CPI m/m at 12:30 UTC", why

    blocked, _ = at(-30)
    assert blocked is True, "the -30 edge is INCLUSIVE"

    blocked, _ = at(-31)
    assert blocked is False, "31 min out is outside the pad"
    assert at(-31)[1] == ""

    blocked, _ = at(29)
    assert blocked is True, "29 min after the release must still black out"

    blocked, _ = at(31)
    assert blocked is False, "31 min after is outside the pad"

    blocked, _ = at(0)
    assert blocked is True, "the release minute itself"

    assert at(-29, importance=0)[0] is False, "medium does not black out at default min_importance=1"

    blocked, why = econ.blackout(now=datetime.fromisoformat("2026-09-17T12:30:00+00:00"),
                                 pad_min=30, min_importance=0)
    assert blocked is True


def test_blackout_picks_nearest_and_respects_countries():
    rows = [
        _ev("2026-09-17T12:00:00Z", "Housing Starts", 1),
        _ev("2026-09-17T12:45:00Z", "Building Permits", 1),
    ]
    install_cache(rows, age_s=0)
    go_offline()
    now = datetime.fromisoformat("2026-09-17T12:40:00+00:00")
    blocked, why = econ.blackout(now=now, pad_min=30)
    assert blocked is True
    assert why == "Building Permits at 12:45 UTC", f"nearest wins, got {why}"

    install_cache([_ev("2026-09-17T12:30:00Z", "German CPI", 1, country="DE")], age_s=0)
    go_offline()
    assert econ.blackout(now=datetime.fromisoformat("2026-09-17T12:30:00+00:00"),
                         countries=("US",))[0] is False


# --------------------------------------------------------------------------- #
# 5. event_day() across the ET calendar-day boundary
# --------------------------------------------------------------------------- #
def test_event_day_across_et_boundary():
    # 2026-09-18T03:00Z == 2026-09-17 23:00 ET -> belongs to the ET day Sep 17.
    late = _ev("2026-09-18T03:00:00Z", "FOMC Statement", 1)
    assert late["date_et"] == "2026-09-17", late["date_et"]

    install_cache([late], age_s=0)
    go_offline()

    hit, why = econ.event_day(now="2026-09-17T20:00:00Z")  # 16:00 ET Sep 17
    assert hit is True and why == "FOMC Statement 03:00 UTC", (hit, why)

    # 21:00 ET on Sep 17 (already Sep 18 in UTC) is still the Sep 17 ET day.
    assert econ.event_day(now="2026-09-18T01:00:00Z")[0] is True

    # 01:00 ET Sep 18 -> the event is now yesterday's; not "today".
    assert econ.event_day(now="2026-09-18T05:00:00Z") == (False, "")

    # An event just AFTER ET midnight must be found even though it is far
    # outside the default now-1h..now+7d window (this is why _et_day_bounds
    # exists instead of reusing events()' default window).
    early = _ev("2026-09-18T04:30:00Z", "Jobless Claims", 1)  # 00:30 ET Sep 18
    assert early["date_et"] == "2026-09-18"
    install_cache([early], age_s=0)
    go_offline()
    hit, why = econ.event_day(now="2026-09-18T06:00:00Z")  # 02:00 ET Sep 18
    assert hit is True, "a 00:30 ET release must still count for the same ET day"
    assert why == "Jobless Claims 04:30 UTC", why

    # ...and the default events() window really would have missed it:
    assert econ.events(from_ts="2026-09-18T05:00:00Z",
                       to_ts="2026-09-18T07:00:00Z") == []


def test_event_day_importance_gate_and_reporting():
    rows = [
        _ev("2026-09-17T12:30:00Z", "Housing Starts", 0),
        _ev("2026-09-17T18:00:00Z", "Fed Interest Rate Decision", 1),
    ]
    install_cache(rows, age_s=0)
    go_offline()
    hit, why = econ.event_day(now="2026-09-17T13:00:00Z", min_importance=0)
    assert hit is True
    assert why == "Fed Interest Rate Decision 18:00 UTC", f"most important wins: {why}"

    only_med = _ev("2026-09-17T12:30:00Z", "Housing Starts", 0)
    install_cache([only_med], age_s=0)
    go_offline()
    assert econ.event_day(now="2026-09-17T13:00:00Z", min_importance=1) == (False, "")
    assert econ.event_day(now="2026-09-17T13:00:00Z", min_importance=0)[0] is True


def test_high_impact_today():
    rows = [
        _ev("2026-09-17T18:00:00Z", "Fed Decision", 1),
        _ev("2026-09-17T12:30:00Z", "CPI m/m", 1),
        _ev("2026-09-17T14:00:00Z", "Philadelphia Fed", 0),
        _ev("2026-09-18T12:30:00Z", "PPI m/m", 1),  # tomorrow ET
    ]
    install_cache(rows, age_s=0)
    go_offline()
    out = econ.high_impact_today(now="2026-09-17T15:00:00Z")
    assert [e["title"] for e in out] == ["CPI m/m", "Fed Decision"], out
    assert all(e["importance"] == 1 for e in out)
    assert all(e["date_et"] == "2026-09-17" for e in out)


# --------------------------------------------------------------------------- #
# 6. size_multiplier
# --------------------------------------------------------------------------- #
def test_size_multiplier():
    ts = "2026-09-17T12:30:00Z"
    install_cache([_ev(ts, "CPI m/m", 1)], age_s=0)
    go_offline()

    assert econ.size_multiplier(now="2026-09-17T12:30:00Z") == 0.0, "inside the pad"
    assert econ.size_multiplier(now="2026-09-17T12:31:00Z") == 0.0
    assert econ.size_multiplier(now="2026-09-17T14:00:00Z") == 0.5, "same ET day, outside the pad"
    assert econ.size_multiplier(now="2026-09-18T14:00:00Z") == 1.0, "quiet day"

    # a HIGH event that already printed earlier today keeps the half-size
    install_cache([_ev("2026-09-17T12:30:00Z", "CPI m/m", 1)], age_s=0)
    go_offline()
    assert econ.size_multiplier(now="2026-09-17T20:00:00Z") == 0.5

    # medium-only day is full size
    install_cache([_ev("2026-09-17T12:30:00Z", "Housing Starts", 0)], age_s=0)
    go_offline()
    assert econ.size_multiplier(now="2026-09-17T20:00:00Z") == 1.0

    # a cross-ET-midnight event (23:00 ET) sizes down the RIGHT ET day
    install_cache([_ev("2026-09-18T03:00:00Z", "FOMC Statement", 1)], age_s=0)
    go_offline()
    assert econ.size_multiplier(now="2026-09-18T02:00:00Z") == 0.5   # 22:00 ET Sep 17
    assert econ.size_multiplier(now="2026-09-18T06:00:00Z") == 1.0   # 02:00 ET Sep 18

    # empty calendar -> full size
    install_cache([], age_s=0)
    go_offline()
    assert econ.size_multiplier(now="2026-09-17T20:00:00Z") == 1.0


# --------------------------------------------------------------------------- #
# 7. brief()
# --------------------------------------------------------------------------- #
def test_brief_format_grouping_and_priority():
    rows = [
        _ev("2026-09-17T12:30:00Z", "Initial Jobless Claims", 0),
        _ev("2026-09-17T14:00:00Z", "Philadelphia Fed", 0),
        _ev("2026-09-17T18:00:00Z", "Fed Interest Rate Decision", 1),
        _ev("2026-09-18T12:30:00Z", "PPI m/m", 1),
        _ev("2026-09-18T13:15:00Z", "Industrial Production", 0),
    ]
    install_cache(rows, age_s=0)
    go_offline()

    text = econ.brief(now="2026-09-17T11:00:00Z", days=3)
    assert text, "brief must not be empty when events exist"
    assert text.startswith("Thu Sep 17: "), text
    assert "Fri Sep 18: " in text, text
    assert "; " in text, "days are joined with '; '"
    assert "12:30Z Initial Jobless Claims (med)" in text, text
    assert "14:00Z Philadelphia Fed (med)" in text, text
    assert "18:00Z Fed Interest Rate Decision (HIGH)" in text, text
    assert "13:15Z Industrial Production (med)" in text, text

    day1 = text.split("; ")[0]
    assert day1.index("(HIGH)") < day1.index("(med)"), "HIGH items must come first"
    assert day1.startswith("Thu Sep 17: 18:00Z Fed Interest Rate Decision (HIGH)"), day1
    # chronological within a tier (not across tiers — HIGH is deliberately first)
    assert day1.index("12:30Z") < day1.index("14:00Z"), day1
    assert day1.index("(med)") < day1.index("14:00Z"), day1
    assert text.index("Thu Sep 17:") < text.index("Fri Sep 18:"), text

    # low tier only appears when explicitly asked for
    rows_low = rows + [_ev("2026-09-17T19:00:00Z", "Fed Speak", -1)]
    install_cache(rows_low, age_s=0)
    go_offline()
    assert "Fed Speak" not in econ.brief(now="2026-09-17T11:00:00Z", days=3)
    assert "(low)" in econ.brief(now="2026-09-17T11:00:00Z", days=3, min_importance=-1)

    # US-only by default
    install_cache([_ev("2026-09-17T12:30:00Z", "German CPI", 1, country="DE")], age_s=0)
    go_offline()
    assert econ.brief(now="2026-09-17T09:00:00Z", days=3) == ""
    assert "German CPI" in econ.brief(now="2026-09-17T09:00:00Z", days=3, countries=None)

    # quiet calendar -> '' (not a placeholder string)
    install_cache([], age_s=0)
    go_offline()
    assert econ.brief(now="2026-09-17T09:00:00Z", days=3) == ""


def test_brief_max_items_and_char_cap():
    many = [_ev(f"2026-09-17T{h:02d}:{m:02d}:00Z",
                "Extremely Verbose Macroeconomic Release Title Number %d" % (h * 4 + m),
                1) for h in range(12, 21) for m in range(0, 60, 15)]
    assert len(many) > 30
    install_cache(many, age_s=0)
    go_offline()

    small = econ.brief(now="2026-09-17T00:00:00Z", days=3, max_items=3)
    assert small.count("Z ") == 3, f"max_items not honoured: {small}"

    big = econ.brief(now="2026-09-17T00:00:00Z", days=3, max_items=200)
    assert len(big) <= 1200, f"brief exceeded the 1200-char cap: {len(big)}"
    assert big.endswith(", …"), f"truncation marker missing: ...{big[-40:]!r}"


def test_day_label():
    assert econ._day_label("2026-09-17") == "Thu Sep 17"
    assert econ._day_label("2026-09-18") == "Fri Sep 18"
    assert econ._day_label("2026-09-01") == "Tue Sep 1", "no zero padding on the day"
    assert econ._day_label("nonsense") == "nonsense"


# --------------------------------------------------------------------------- #
# 8. FAIL OPEN — the contract the bots depend on
# --------------------------------------------------------------------------- #
def test_fail_open_corrupt_cache_no_exception():
    path = _TMP / "corrupt.json"
    path.write_text("{not valid json at all", encoding="utf-8")
    econ.CACHE_PATH = path
    go_offline()

    assert econ.events() == []
    assert econ.events(min_importance=-1, countries=None) == []
    assert econ.brief() == ""
    assert econ.brief(now="2026-09-17T12:00:00Z") == ""
    assert econ.blackout() == (False, "")
    assert econ.blackout(now="2026-09-17T12:00:00Z") == (False, "")
    assert econ.event_day() == (False, "")
    assert econ.high_impact_today() == []
    assert econ.size_multiplier() == 1.0
    out, printed = quiet(econ.refresh, force=True)
    assert out["source"] == "none", out["source"]
    assert out["events"] == []
    assert "econ:" in printed, "refresh must print a one-line warning"

    # also: a cache that is valid JSON but the wrong shape / wrong types
    for junk in ('{"events": "not-a-list"}', '[]', 'null', '{"events": [{"ts": 3}]}',
                 '{"fetched_at": "2026-09-17T00:00:00Z", "events": [1, 2, 3]}'):
        path.write_text(junk, encoding="utf-8")
        econ.CACHE_PATH = path
        go_offline()
        assert econ.events() == [], junk
        assert econ.size_multiplier(now="2026-09-17T12:00:00Z") == 1.0, junk


def test_fail_open_unparseable_now():
    install_cache(TIERS, age_s=0)
    go_offline()
    bad = "definitely-not-a-timestamp"
    # an unparseable window degrades to the default window; it must not raise
    assert isinstance(econ.events(from_ts=bad, to_ts=bad), list)
    assert econ.brief(now=bad) == ""
    assert econ.blackout(now=bad) == (False, "")
    assert econ.event_day(now=bad) == (False, "")
    assert econ.high_impact_today(now=bad) == []
    assert econ.size_multiplier(now=bad) == 1.0
    # wrong TYPES for the window args must not raise either
    assert isinstance(econ.events(from_ts=None, to_ts=12345), list)
    assert isinstance(econ.events(from_ts=12345, to_ts=None), list)
    assert econ.brief(now=datetime.now(UTC), days=3) is not None


def test_default_window_is_now_minus_1h_to_plus_7d():
    """Default window regression: an event 2h old is out, 3 days out is in."""
    now = datetime.now(UTC)
    rows = [
        _ev(_iso(now - timedelta(minutes=90)), "Too Old", 1),
        _ev(_iso(now - timedelta(minutes=30)), "Just Fired", 1),
        _ev(_iso(now + timedelta(days=3)), "Midweek", 1),
        _ev(_iso(now + timedelta(days=9)), "Too Far", 1),
    ]
    install_cache(rows, age_s=0)
    go_offline()
    titles = [e["title"] for e in econ.events()]
    assert titles == ["Just Fired", "Midweek"], titles


def test_stale_cache_still_gates_trades():
    """Refresh failure must NOT discard a stale cache — the bots keep gating."""
    stale = [_ev("2026-09-17T12:30:00Z", "CPI m/m", 1)]
    install_cache(stale, age_s=econ.CACHE_TTL_S + 3600)  # expired
    go_offline()
    assert econ.events(from_ts=WIN_FROM, to_ts=WIN_TO, min_importance=1), \
        "stale cache must still be served"
    assert econ.blackout(now="2026-09-17T12:30:00Z")[0] is True
    assert econ.event_day(now="2026-09-17T13:00:00Z")[0] is True


def test_fresh_cache_avoids_repeated_fetches():
    """A fresh cache must not trigger a fetch — force nothing, stay offline."""
    install_cache(TIERS, age_s=0)
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        raise RuntimeError("must not be called")

    econ._SOURCES = (("tradingview", counting), ("forexfactory", counting))
    econ._last_fetch_fail = 0.0
    for _ in range(3):
        econ.events()
        econ.brief(now="2026-09-17T12:00:00Z")
    assert calls["n"] == 0, "a fresh (<6h) cache must be used without any fetch"

    out, _ = quiet(econ.refresh)  # force=False, cache fresh -> source 'cache'
    assert out["source"] == "cache", out["source"]
    assert calls["n"] == 0


def test_refresh_writes_cache_and_falls_through_sources():
    """A failing primary must fall through to the fallback source."""
    path = _TMP / "fallthrough.json"
    econ.CACHE_PATH = path
    econ._last_fetch_fail = 0.0

    def dead_primary():
        raise RuntimeError("tradingview unreachable")

    def good_fallback():
        return econ.parse_ff_xml(FF_XML)

    econ._SOURCES = (("tradingview", dead_primary), ("forexfactory", good_fallback))
    out, printed = quiet(econ.refresh, force=True)
    assert out["source"] == "forexfactory", out["source"]
    assert "tradingview fetch failed" in printed, printed
    assert path.exists(), "refresh must write the cache on success"

    written = json.loads(path.read_text())
    assert written["source"] == "forexfactory"
    assert len(written["events"]) == 4
    assert set(written["events"][0]) == {"ts", "date_et", "title", "country",
                                         "importance", "forecast", "previous",
                                         "actual"}

    # an empty (but successful) payload is a soft failure -> stale cache stands
    econ._SOURCES = (("tradingview", lambda: []), ("forexfactory", lambda: []))
    prior = path.read_text()
    out, printed = quiet(econ.refresh, force=True)
    assert out["source"] == "none", out["source"]
    assert path.read_text() == prior, "an empty week must not overwrite the cache"
    assert "no calendar source available" in printed


def test_real_transport_failure_is_fail_open():
    """The REAL network path: unresolvable host -> retry -> give up, no raise.

    .invalid is reserved (RFC 2606) so this never depends on the internet, but
    it does exercise _http_get's retry-and-sleep logic and refresh()'s total
    failure branch.
    """
    path = _TMP / "no_net.json"
    path.unlink(missing_ok=True)
    econ.CACHE_PATH = path
    econ._SOURCES = (
        ("tradingview", econ._fetch_tradingview),
        ("forexfactory", econ._fetch_forexfactory),
    )
    saved = (econ.TV_URL, econ.FF_URL, econ._HTTP_TIMEOUT)
    econ.TV_URL = "https://no-such-econ-host.invalid/events"
    econ.FF_URL = "https://no-such-econ-host.invalid/ff.xml"
    econ._HTTP_TIMEOUT = 3
    econ._last_fetch_fail = 0.0
    try:
        out, printed = quiet(econ.refresh, force=True)
        assert out["source"] == "none", out["source"]
        assert out["events"] == []
        assert "no calendar source available" in printed, printed
        assert not path.exists(), "a total failure must not write an empty cache"

        econ._last_fetch_fail = 0.0
        assert econ.events() == []
        assert econ.brief() == ""
        assert econ.blackout() == (False, "")
        assert econ.event_day() == (False, "")
        assert econ.high_impact_today() == []
        assert econ.size_multiplier() == 1.0
    finally:
        econ.TV_URL, econ.FF_URL, econ._HTTP_TIMEOUT = saved


def test_failure_backoff_limits_network_attempts():
    """After one failed refresh, repeated checks must not re-hit the network."""
    path = _TMP / "backoff.json"
    path.unlink(missing_ok=True)
    econ.CACHE_PATH = path
    calls = {"n": 0}

    def counting_fail():
        calls["n"] += 1
        raise RuntimeError("down")

    econ._SOURCES = (("tradingview", counting_fail), ("forexfactory", counting_fail))
    econ._last_fetch_fail = 0.0
    for _ in range(5):
        econ.blackout(now="2026-09-17T12:00:00Z")
    assert calls["n"] == 2, f"2 fetchers x 1 attempt = 2, got {calls['n']}"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    checks = [
        ("importance scale not inverted", test_importance_scale_not_inverted),
        ("tv payload keeps all three tiers", test_tv_payload_keeps_all_three_tiers),
        ("tv numbers strip trailing .0", test_tv_number_formatting_strips_trailing_dot_zero),
        ("tv bad envelope raises for refresh", test_tv_bad_envelope_raises_for_refresh_to_catch),
        ("ff xml ET->UTC conversion", test_ff_xml_et_to_utc_conversion),
        ("ff impact mapping table", test_ff_impact_mapping_table),
        ("ff DST winter vs summer offset", test_ff_dst_winter_offset),
        ("ff time parsing edges", test_ff_time_parsing_edges),
        ("ff malformed xml does not escape", test_ff_xml_malformed_bytes_do_not_escape),
        ("min_importance filtering", test_min_importance_filtering),
        ("event dict shape is exact", test_event_dict_shape_is_exact),
        ("events window + country filters", test_events_window_and_country_filters),
        ("events sorted ascending", test_events_sorted_ascending),
        ("blackout window edges", test_blackout_window_edges),
        ("blackout nearest + countries", test_blackout_picks_nearest_and_respects_countries),
        ("event_day across ET boundary", test_event_day_across_et_boundary),
        ("default window now-1h..now+7d", test_default_window_is_now_minus_1h_to_plus_7d),
        ("event_day importance gate", test_event_day_importance_gate_and_reporting),
        ("high_impact_today", test_high_impact_today),
        ("size_multiplier", test_size_multiplier),
        ("brief format/grouping/priority", test_brief_format_grouping_and_priority),
        ("brief max_items + char cap", test_brief_max_items_and_char_cap),
        ("day label", test_day_label),
        ("fail open: corrupt cache", test_fail_open_corrupt_cache_no_exception),
        ("fail open: unparseable now", test_fail_open_unparseable_now),
        ("stale cache still gates trades", test_stale_cache_still_gates_trades),
        ("fresh cache avoids repeat fetches", test_fresh_cache_avoids_repeated_fetches),
        ("refresh writes cache + falls through", test_refresh_writes_cache_and_falls_through_sources),
        ("real transport failure is fail open", test_real_transport_failure_is_fail_open),
        ("failure backoff limits attempts", test_failure_backoff_limits_network_attempts),
    ]

    print(f"econ.py test suite — {len(checks)} checks")
    print(f"module:  {econ.__file__}")
    print(f"cache path under test: {econ.CACHE_PATH}")
    print("-" * 72)
    for name, fn in checks:
        run(name, fn)
    print("-" * 72)
    print(f"{len(_PASS)} passed, {len(_FAIL)} failed")
    for name, err in _FAIL:
        print(f"  FAILED {name}: {err}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
