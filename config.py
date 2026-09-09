"""Trader profile — central config for the Alpaca trading bot system.

One place for every tunable. Strategy params are DATA (knobs), not logic: the
nightly self-improvement routine may PROPOSE changes AND, since Sep 2026, is
authorised to AUTO-APPLY tunable knobs within the safety floor (writes to
strategy_overrides.json, which config loads at start). It may ONLY tune the
whitelisted knobs below and never disable the irreducible floor (stop-loss,
retail-only, BTC-only).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# App version (bumped manually on releases; shown in the dashboard footer).
VERSION = "1.2.4"

# --------------------------------------------------------------------------- #
# Paths. PROFILE_HOME is pinned to the trader profile (override via TRADER_HOME)
# so scripts behave identically regardless of the ambient HERMES_HOME.
# --------------------------------------------------------------------------- #
PROFILE_HOME = Path(os.environ.get("TRADER_HOME", "/opt/data/profiles/trader"))
KEYS_FILE = PROFILE_HOME / ".alpaca_keys.env"
TRADING_DIR = PROFILE_HOME / "trading"
DATA_DIR = TRADING_DIR / "data"
STATE_DIR = DATA_DIR / "state"
DB_PATH = DATA_DIR / "trades.db"
DASHBOARD_PATH = TRADING_DIR / "dashboard" / "index.html"
LESSONS_PATH = TRADING_DIR / "lessons_learned.md"
PROPOSALS_PATH = TRADING_DIR / "strategy_proposals.md"
# Runtime knob overrides written by the self-improvement routine (and read back
# at import). A human may also edit this file directly — it wins over source.
OVERRIDES_PATH = STATE_DIR / "strategy_overrides.json"


def _load_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    if KEYS_FILE.exists():
        for line in KEYS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            keys[k.strip()] = v.strip()
    # Environment variables supplement/override the key file, so a container can
    # inject secrets as env vars instead of mounting a key file.
    for name in ("ALPACA_DAILY_API_KEY", "ALPACA_DAILY_SECRET_KEY",
                 "ALPACA_WEEKLY_API_KEY", "ALPACA_WEEKLY_SECRET_KEY",
                 "ALPACA_YOLO_API_KEY", "ALPACA_YOLO_SECRET_KEY"):
        if os.environ.get(name):
            keys[name] = os.environ[name]
    return keys


_KEYS = _load_keys()

# --------------------------------------------------------------------------- #
# Accounts (three separate paper accounts)
# --------------------------------------------------------------------------- #
ACCOUNTS = {
    "daily": {
        "base_url": _KEYS.get("ALPACA_DAILY_BASE_URL", "https://paper-api.alpaca.markets/v2"),
        "api_key": _KEYS.get("ALPACA_DAILY_API_KEY", ""),
        "secret_key": _KEYS.get("ALPACA_DAILY_SECRET_KEY", ""),
        "paper": True,
    },
    "weekly": {
        "base_url": _KEYS.get("ALPACA_WEEKLY_BASE_URL", "https://paper-api.alpaca.markets/v2"),
        "api_key": _KEYS.get("ALPACA_WEEKLY_API_KEY", ""),
        "secret_key": _KEYS.get("ALPACA_WEEKLY_SECRET_KEY", ""),
        "paper": True,
    },
    "yolo": {
        "base_url": _KEYS.get("ALPACA_YOLO_BASE_URL", "https://paper-api.alpaca.markets/v2"),
        "api_key": _KEYS.get("ALPACA_YOLO_API_KEY", ""),
        "secret_key": _KEYS.get("ALPACA_YOLO_SECRET_KEY", ""),
        "paper": True,
    },
}

# Market Data API host (bars/quotes/trades/news). Same host for paper & live;
# the paper-vs-live distinction is carried by the API key, not the host.
DATA_BASE_URL = "https://data.alpaca.markets"

# --------------------------------------------------------------------------- #
# Tradable universe (IRREDUCIBLE floor — never disabled by an override)
# --------------------------------------------------------------------------- #
# Retail-accessible instruments only. NO crypto except Bitcoin. These keys are
# pinned below (and re-pinned by _load_overrides) so self-improvement cannot
# widen the universe. You explicitly told me this stays.
CRYPTO_ALLOWLIST = ["BTC/USD"]
ALLOWED_ASSETS = ["us_equity", "us_etf", "us_option"]


# --------------------------------------------------------------------------- #
# LLM (decision layer)
# --------------------------------------------------------------------------- #
def _load_env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = PROFILE_HOME / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


_LLM_ENV = _load_env_file()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or _LLM_ENV.get("DEEPSEEK_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-pro")
LLM_BASE_URL = "https://api.deepseek.com/v1"

# --------------------------------------------------------------------------- #
# Global risk parameters
# --------------------------------------------------------------------------- #
# These are SUGGESTIONS the LLM/strategies may deviate from within the safety
# floor (they are tune-able via overrides). The irreducible floor is above.
RISK_PCT_PER_TRADE = 0.01          # default per-trade risk (LLM may size up/down)
MAX_DAILY_LOSS_PCT = 0.015         # advisory circuit breaker (not hard-enforced)
MIN_NET_RR = 0.5                   # sanity floor: reject net-negative-after-fees trades
TUNE_BOUNDS = {"RISK_PCT_PER_TRADE": (0.002, 0.05), "MIN_NET_RR": (0.2, 3.0)}

# US market timezone (Alpaca clocks are in this tz)
MARKET_TZ = "America/New_York"

# --------------------------------------------------------------------------- #
# Daily strategy — suggested framework (evolves via self-improvement)
# --------------------------------------------------------------------------- #
DAILY = {
    "symbol": "SPY",
    "orb_start": "09:30",     # opening-range window (ET)
    "orb_end": "10:00",
    "rr_multiple": 1.5,       # preferred target multiple (a knob, not a rule)
    "close_confirmation": True,  # advisory: require a CLOSE beyond the range
    # NO flat_by: a position may be held overnight (the bot's own decision).
    # NO max_trades_per_day: the LLM + risk gate decide timing, not a cap.
    "time_in_force": "gtc",   # bracket legs survive 20:00 ET -> overnight protected
    "max_trades_per_day": 3,  # loose ceiling so a bug can't re-enter every tick
}
DAILY_TUNE = {
    "rr_multiple": (0.5, 3.0),
    "orb_start": None, "orb_end": None,   # times: validated as HH:MM, not ranged
    "close_confirmation": (None, None),   # boolean, validated as bool
    "max_trades_per_day": (1, 5),
}

# --------------------------------------------------------------------------- #
# Weekly strategy — suggested framework (evolves via self-improvement)
# --------------------------------------------------------------------------- #
WEEKLY = {
    "symbol": "SPY",
    "trend_sma_weeks": 20,
    "atr_period": 14,
    "atr_timeframe": "1Day",
    "stop_atr": 1.0,
    "target_atr": 1.5,
    # NO max_hold_days: hold as long as the stop/target bracket says.
    # NO max_positions: the (single-symbol) slot is free to hold what it likes.
}
WEEKLY_TUNE = {
    "trend_sma_weeks": (10, 100),
    "atr_period": (5, 40),
    "stop_atr": (0.5, 3.0),
    "target_atr": (0.5, 5.0),
}

# --------------------------------------------------------------------------- #
# YOLO / "NoRulesRules" — full autonomy config
# --------------------------------------------------------------------------- #
# SAFETY_FLOOR keeps a minimal risk floor even for the fully-autonomous trader.
# Set False for a truly unconstrained bot (NOT recommended, even on paper).
YOLO = {
    "safety_floor": True,
    "max_position_pct": 0.6,        # max notional in one position (% of equity) — relaxed
    "max_risk_pct": 0.05,           # max entry->stop risk per trade (% of equity) — relaxed
    "max_concurrent_positions": 10, # relaxed
    "require_stop_loss": True,      # IRREDUCIBLE (re-pinned below)
    "allowed_assets": ALLOWED_ASSETS,  # retail-accessible only (IRREDUCIBLE)
    "crypto_allowlist": CRYPTO_ALLOWLIST,  # crypto: BTC only (IRREDUCIBLE)
    # Bracket-geometry buffers: all stops/targets are validated against the LIVE
    # price (Alpaca 422s any stop within $0.01 of it). These are the minimum
    # entry->stop / entry->target distances as a % of the live price. OPERATIONAL
    # safety — never tune via overrides.
    "min_stop_dist_pct": 0.002,
    "min_target_dist_pct": 0.001,
    # v1 auto-trade universe (liquid, retail-accessible US equities/ETFs).
    # Options and BTC/USD are excluded from v1 auto-execution because they need
    # the options-chain / crypto market-data endpoints, which are follow-ups.
    "watchlist": [
        "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "IEF", "HYG",
        "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "SMH",
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    ],
}
YOLO_TUNE = {
    "max_position_pct": (0.05, 0.9),
    "max_risk_pct": (0.01, 0.1),
    "max_concurrent_positions": (1, 10),
}

# --------------------------------------------------------------------------- #
# Self-healing (OPERATIONAL knobs — NOT strategy; safe for autonomous runs)
# --------------------------------------------------------------------------- #
# selfheal.py restores intended broker state when runtime anomalies break it
# (a GTC bracket leg cancelled leaving a naked position, orphaned protective
# orders on flat symbols, DB drift vs broker). It never opens NEW trades and
# never invents risk levels: protective prices are copied verbatim from the
# trade's own stop_price/target_price. Reconcile (fills/P&L) is separate;
# selfheal fixes STATE. Hermes monitors the outcome.
SELFHEAL = {
    "enabled": True,
    "adopt_missing_rows": True,    # R1: create DB row for a broker position missing one
    "reattach_protection": True,   # R2: re-place stop(+target) legs for naked positions
    "cancel_debris": True,         # R3: cancel orphan protective orders on flat symbols
    "void_unfilled": True,         # R4: void DB rows whose broker order cancelled unfilled
    "open_tif": "day",             # TIF for re-attached protection during market hours
    "after_hours_tif": "gtc",      # TIF after the close (protects the overnight hold)
}

# --------------------------------------------------------------------------- #
# Runtime knob overrides (written by self_improve, read at import here)
# --------------------------------------------------------------------------- #
# __TUNE__: map of KNOWN auto-tune knobs -> (lo, hi) allowed bounds, or None for
# value-validated (bool/time) knobs. Any knob NOT in a __TUNE__ map (or any knob
# in the irreducible floor) is IGNORED by _load_overrides — a hostile or buggy
# override file cannot turn off the stop-loss or widen the universe.
__TUNE__: dict[str, dict] = {
    "RISK_PCT_PER_TRADE": TUNE_BOUNDS["RISK_PCT_PER_TRADE"],
    "MIN_NET_RR": TUNE_BOUNDS["MIN_NET_RR"],
    "DAILY": DAILY_TUNE,
    "WEEKLY": WEEKLY_TUNE,
    "YOLO": YOLO_TUNE,
}
_IRREDUCIBLE = {"require_stop_loss", "allowed_assets", "crypto_allowlist",
                "min_stop_dist_pct", "min_target_dist_pct", "symbol"}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _apply_overrides() -> None:
    """Merge strategy_overrides.json into this module, clamping every value.

    Only whitelisted knobs (in __TUNE__) are honoured; every value is clamped to
    its allowed range; boolean/time knobs are type-validated; and the irreducible
    floor keys are re-pinned no matter what the file says. Fail-closed: a malformed
    override file is ignored (the source config stands).
    """
    if not OVERRIDES_PATH.exists():
        return
    try:
        raw = json.loads(OVERRIDES_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(raw, dict):
        return

    # --- global scalars (assign into this module's global namespace) ---
    g = globals()
    for key, bounds in __TUNE__.items():
        if key in ("DAILY", "WEEKLY", "YOLO") or key not in raw:
            continue
        lo, hi = bounds
        try:
            g[key] = _clamp(raw[key], lo, hi)
        except (TypeError, ValueError):
            continue

    # --- strategy dicts ---
    for section, bound_map in (("DAILY", DAILY_TUNE), ("WEEKLY", WEEKLY_TUNE),
                               ("YOLO", YOLO_TUNE)):
        section_tune = __TUNE__.get(section, {})
        over = raw.get(section)
        if not isinstance(over, dict):
            continue
        target = {"DAILY": DAILY, "WEEKLY": WEEKLY, "YOLO": YOLO}[section]
        for key, val in over.items():
            if key in _IRREDUCIBLE or key not in section_tune:
                continue  # never touch the floor or an unknown knob
            bounds = section_tune[key]
            if bounds is None:
                continue
            lo, hi = bounds
            if lo is None or hi is None:  # boolean knob
                target[key] = bool(val)
            else:
                try:
                    target[key] = _clamp(val, lo, hi)
                except (TypeError, ValueError):
                    continue
    # Re-pin the irreducible floor (belt-and-braces).
    YOLO["require_stop_loss"] = True
    YOLO["allowed_assets"] = ALLOWED_ASSETS
    YOLO["crypto_allowlist"] = CRYPTO_ALLOWLIST


# Apply runtime overrides when this module is imported.
_apply_overrides()


def tunable_knobs() -> list[dict]:
    """Return the auto-tune whitelist as [{'key','section','current','lo','hi','type'}].

    'type' is 'num', 'bool', or 'time'. Only knobs in this list may ever be
    changed by the self-improvement routine; the irreducible floor is absent by
    construction. 'current' is the live (override-resolved) value.
    """
    out: list[dict] = []
    for sec, target, tune in (
            ("", None, {"RISK_PCT_PER_TRADE": TUNE_BOUNDS["RISK_PCT_PER_TRADE"],
                        "MIN_NET_RR": TUNE_BOUNDS["MIN_NET_RR"]}),
            ("DAILY", DAILY, DAILY_TUNE),
            ("WEEKLY", WEEKLY, WEEKLY_TUNE),
            ("YOLO", YOLO, YOLO_TUNE)):
        for key, bounds in tune.items():
            if bounds is None:
                continue
            lo, hi = bounds
            if lo is None or hi is None:  # boolean knob
                vtype = "bool"
                cur = bool(target[key]) if target else globals()[key]
            else:
                vtype = "num"
                cur = target[key] if target else globals()[key]
            out.append({"key": key, "section": sec, "current": cur,
                        "lo": lo, "hi": hi, "type": vtype})
    return out


# Time knobs that must validate as HH:MM (validated by shape, not range).
_TIME_KEYS = {"orb_start", "orb_end"}


def _coerce(val, bounds, current):
    lo, hi = bounds
    if val is None:
        return current
    if lo is None or hi is None:
        return bool(val)
    return _clamp(val, lo, hi)


def _flat_knob(key: str):
    """Resolve a bare knob name -> (section, bounds, current) or None."""
    for sec, target, tune in (
            ("", None, {"RISK_PCT_PER_TRADE": TUNE_BOUNDS["RISK_PCT_PER_TRADE"],
                        "MIN_NET_RR": TUNE_BOUNDS["MIN_NET_RR"]}),
            ("DAILY", DAILY, DAILY_TUNE),
            ("WEEKLY", WEEKLY, WEEKLY_TUNE),
            ("YOLO", YOLO, YOLO_TUNE)):
        if key in tune:
            bounds = tune[key]
            if bounds is None:
                return None  # time knob — not auto-tuned
            cur = globals()[key] if sec == "" else target[key]
            return sec, bounds, cur
    return None


def _tokenize(candidate: dict) -> dict:
    """Validate + clamp a self-improvement candidate override -> clean dict.

    Only whitelisted knobs are kept (bounds from __TUNE__); the irreducible
    floor keys are dropped; numeric knobs are clamped; bool knobs are coerced;
    unknown / time knobs dropped. Honors `<SECTION> -> {key: val}` and flat
    `key: val` shapes. Returns a clean, safe override dict (never raises)."""
    clean: dict = {}
    for key, val in candidate.items():
        if isinstance(val, dict):  # <SECTION>: {...}
            sec = key.upper()
            if sec not in ("DAILY", "WEEKLY", "YOLO"):
                continue
            target = {"DAILY": DAILY, "WEEKLY": WEEKLY, "YOLO": YOLO}[sec]
            tune = {"DAILY": DAILY_TUNE, "WEEKLY": WEEKLY_TUNE, "YOLO": YOLO_TUNE}[sec]
            for k, v in val.items():
                if k in _IRREDUCIBLE or k not in tune or tune[k] is None:
                    continue
                try:
                    clean.setdefault(sec, {})[k] = _coerce(v, tune[k], target.get(k))
                except (TypeError, ValueError):
                    continue
        else:  # flat knob
            hit = _flat_knob(key)
            if hit is None:
                continue
            sec, bounds, cur = hit
            if key in _IRREDUCIBLE:
                continue
            try:
                cv = _coerce(val, bounds, cur)
            except (TypeError, ValueError):
                continue
            if sec == "":
                clean[key] = cv
            else:
                clean.setdefault(sec, {})[key] = cv
    return clean


def propose_override(candidate: dict) -> tuple[dict, list[str]]:
    """Validate a self-improvement candidate -> (clean_overrides, notes[]).

    Notes describe what was clamped or dropped, so the appliance is auditable.
    """
    notes: list[str] = []
    # Detect attempts to touch the floor (audit trail, but never applied).
    for sec in ("DAILY", "WEEKLY", "YOLO"):
        for k in _IRREDUCIBLE:
            if isinstance(candidate.get(sec), dict) and k in candidate[sec]:
                notes.append(f"DROPPED irreducible floor knob {sec}.{k}")
    for k in _IRREDUCIBLE:  # flat floor keys, e.g. {'require_stop_loss': False}
        if k in candidate:
            notes.append(f"DROPPED irreducible floor knob {k}")
    clean = _tokenize(candidate)
    return clean, notes
