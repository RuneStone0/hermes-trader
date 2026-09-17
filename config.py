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
VERSION = "1.5.0"

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

# Starting capital per paper account — the baseline for the Net P/L % return
# (realized net P/L ÷ starting capital). Seeded once into account_state by
# reconcile and never overwritten, so it stays a true lifetime baseline.
STARTING_CAPITAL = {"daily": 10000.0, "weekly": 10000.0, "yolo": 10000.0}

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
# NOTE: this reads the loaded .env too, not just os.environ. Previously it was
# os.environ-only, so setting LLM_MODEL in .env silently did nothing — the file
# was loaded (DEEPSEEK_API_KEY uses it) but this line ignored it, and the stale
# hardcoded default won. Keep the .env fallback: it is the deployment's knob and
# the docker-compose path passes the same name as a real env var.
LLM_MODEL = os.environ.get("LLM_MODEL") or _LLM_ENV.get("LLM_MODEL") or "deepseek-flash"
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
    # How far PAST the range edge price may be before the breakout counts as
    # stale and is passed on. The plan's risk is one range wide (edge to edge),
    # but the entry is a market order that fills at the current price while the
    # stop stays pinned to the opposite edge — so entering a 10:05 breakout at
    # 15:30 would risk several multiples of the intended size. 0.25% keeps the
    # realised risk within a few percent of the plan.
    "max_chase_pct": 0.25,
    # The ORB has NO demonstrated edge: the walk-forward backtest
    # (reports/backtest_2026-09.md) measured avgR -0.01 over 57 real 5-min trades
    # (t -0.09) and -0.06 over a 327-trade daily proxy (t -0.69), with a 95% CI
    # straddling zero either way. That is not proof it loses — the sample is too
    # small to say — so it keeps trading and keeps gathering evidence, but at HALF
    # the risk of the sleeve that does have evidence (MR). Revisit when it has 30+
    # closed trades of its own; that is the weekly review's job, not a guess now.
    "risk_pct": 0.005,
}
DAILY_TUNE = {
    "rr_multiple": (0.5, 3.0),
    "orb_start": None, "orb_end": None,   # times: validated as HH:MM, not ranged
    "close_confirmation": (None, None),   # boolean, validated as bool
    "max_trades_per_day": (1, 5),
    "max_chase_pct": (0.05, 2.0),
    "risk_pct": (0.002, 0.02),
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
# Mean-reversion sleeve (mr_run.py) — added 2026-09-17 ON BACKTEST EVIDENCE
# --------------------------------------------------------------------------- #
# The three original bots were all trend/breakout rules and, measured over the
# Sep 1-17 2026 chop tape, produced 9 trades and no edge. A stdlib walk-forward
# backtest over 2019-2026 (trading/backtest_mr.py, reports/backtest_2026-09.md)
# found the opposite ranking:
#
#   ORB_5min (the live rule, 4 months of 5-min data)  n=57   avgR -0.01  t -0.09
#   ORB_daily proxy                                   n=327  avgR -0.06  t -0.69
#   PULLBACK_weekly                                   n=76   avgR +0.11  t +0.77
#   MR_rsi2  (this sleeve)                            n=775  avgR +0.12  t +6.33
#   MR_atr_dip                                        n=177  avgR +0.21  t +3.60
#   CTRL_long_beta (just hold ETFs in an uptrend)     n=2434 avgR +0.04  t +3.65
#
# The decisive test is the LAST row: long US equity ETFs in an uptrend make money
# under almost any entry rule, so a positive expectancy alone proves nothing. MR's
# edge over that control is +0.078R per trade (Welch t +3.71, 95% CI [+0.04,+0.12]
# — excludes zero), and it survives 2 bp/side slippage. MR_rsi2 is preferred over
# the higher-avgR MR_atr_dip because it carries 4x the sample, the higher t, a
# positive sign in BOTH regimes (+0.15 chop / +0.11 trend) and both walk-forward
# halves (+0.05 / +0.16); MR_atr_dip fired 0 times in the live window.
#
# HONEST CAVEATS, all of them relevant: 2019-2026 was one long bull market for
# these ETFs; the control's trades overlap the MR trades in time (so its Welch t
# understates the uncertainty); pooling 19 symbols ignores the fact that dips
# cluster market-wide; and the shape is fragile — ~70% wins but avg win +0.37R
# against avg loss -0.57R, i.e. the losses are fat when they come. Hence the
# modest risk budget and the concurrency cap below, not max size.
MR = {
    "enabled": True,
    "variant": "rsi2",              # rsi2 | atr_dip (see backtest_mr.py)
    "risk_pct": 0.0075,             # per trade, % of equity
    "max_concurrent": 2,            # dips cluster; cap the correlated basket
    "max_position_pct": 0.20,       # notional ceiling per position
    "rsi_period": 2,
    "rsi_entry": 10.0,              # RSI(2) below this = the dip
    "trend_sma": 200,               # close must be above this
    "slope_sma": 50,                # ...and this one must be rising
    "exit_sma": 5,                  # exit on a close above SMA(5)
    "max_hold_days": 10,            # time stop (the backtest's max_hold)
    "atr_period": 14,
    "stop_atr": 2.5,                # hard stop distance, in ATR(14)
    # The backtest had NO take-profit (exits were the rule, the stop or the time
    # stop). Alpaca brackets require a target leg, so this is a deliberately WIDE
    # outer cap that the rule exit normally beats to it — documented deviation,
    # not a tuned parameter.
    "target_atr": 4.0,
    # Entries are evaluated only in the last 25 minutes of the session, because
    # the tested signal is "the CLOSE is a dip" — not "the price dipped at some
    # point during the day". Running the check all day would enter on intraday
    # wicks the backtest never saw. The forming bar's close is the live price.
    "entry_window_et": ["15:30", "15:55"],
    # SPY is excluded on purpose: the daily ORB owns SPY on this account, and two
    # sleeves holding the same symbol makes attribution (and bracket integrity)
    # impossible. This is a plumbing choice, not a verdict on the evidence.
    "universe": [
        "QQQ", "IWM", "DIA", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU",
        "XLB", "XLE", "SMH", "GLD", "SLV", "TLT", "IEF", "HYG",
    ],
}
MR_TUNE = {
    "risk_pct": (0.002, 0.02),
    "max_concurrent": (1, 5),
    "rsi_entry": (2.0, 25.0),
    "exit_sma": (2, 20),
    "max_hold_days": (3, 30),
    "stop_atr": (1.0, 4.0),
}

# --------------------------------------------------------------------------- #
# YOLO / "NoRulesRules" — full autonomy config
# --------------------------------------------------------------------------- #
# SAFETY_FLOOR keeps a minimal risk floor even for the fully-autonomous trader.
# Set False for a truly unconstrained bot (NOT recommended, even on paper).
YOLO = {
    "safety_floor": True,
    # Sizing DEFAULTS (restored 2026-09-17). The nightly auto-tuner had walked
    # these down to 0.05 / 0.01 / 1 on the evidence of five-to-seven trades —
    # see risk_gov.py for the full post-mortem. Flinch-based de-risking is now
    # replaced by the drawdown governor (RISK_GOV), which is state-based and
    # re-risks automatically when the account recovers. These values are a
    # starting point: big enough that a correct call is visible in the account
    # (a 25% notional / 2% risk trade on $10k moves ~$120-200), small enough
    # that a twenty-trade losing streak is survivable.
    "max_position_pct": 0.25,       # max notional in one position (% of equity)
    "max_risk_pct": 0.02,           # max entry->stop risk per trade (% of equity)
    "max_concurrent_positions": 4,  # still a bounded book, not a scattergun
    "require_stop_loss": True,      # IRREDUCIBLE (re-pinned below)
    "allowed_assets": ALLOWED_ASSETS,  # retail-accessible only (IRREDUCIBLE)
    "crypto_allowlist": CRYPTO_ALLOWLIST,  # crypto: BTC only (IRREDUCIBLE)
    # Bracket-geometry buffers: all stops/targets are validated against the LIVE
    # price (Alpaca 422s any stop within $0.01 of it). These are the minimum
    # entry->stop / entry->target distances as a % of the live price. OPERATIONAL
    # safety — never tune via overrides.
    "min_stop_dist_pct": 0.002,
    "min_target_dist_pct": 0.001,
    # Refuse a NEW single-name entry when the name reports earnings within this
    # many days: a GTC stop does not protect an earnings gap, and the bot holds
    # overnight by default. ETF positions (no earnings) are unaffected. 0 disables.
    "block_earnings_within_days": 1,
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
# Risk governor (DETERMINISTIC — reducing-only, and deliberately NOT tunable)
# --------------------------------------------------------------------------- #
# Replaces flinch-based de-risking: instead of shrinking size every time the
# bot loses (which reacted to a 5-7 trade losing streak — noise for any 2:1
# system), this de-risks on DRAWdown below the account's high-water mark and
# automatically re-risks as equity recovers. See risk_gov.py.
#
# `dd_bands` = ordered (max_drawdown_pct, size_multiplier); the first band the
# current drawdown fits inside wins, so the last entry is the deep floor.
# `hard_stop_dd_pct` blocks all NEW entries beyond that drawdown.
# `daily_loss_cap_pct` blocks new entries for the rest of the session once the
# day's P/L (realized + unrealized, from equity vs last_equity) is this negative.
# `max_gross_exposure_pct` is an account-wide notional ceiling across positions.
#
# THESE ARE RISK CONTROLS, NOT STRATEGY. The auto-tuner can only ever propose
# them (PENDING REVIEW) — an autonomous loop able to loosen its own brakes is
# not self-improvement.
RISK_GOV = {
    "enabled": True,
    "dd_bands": [(3.0, 1.0), (6.0, 0.6), (10.0, 0.35), (15.0, 0.20)],
    "hard_stop_dd_pct": 15.0,
    "daily_loss_cap_pct": 1.5,
    "max_gross_exposure_pct": 150.0,
}

# --------------------------------------------------------------------------- #
# Event awareness (economic calendar) — econ.py
# --------------------------------------------------------------------------- #
# The bots used to decide with zero knowledge of the macro calendar: CPI printed
# Mon 2026-09-14 12:30 UTC and the FOMC decision Wed 2026-09-16 18:00 UTC while
# entries were being placed straight through both, blind. This block makes the
# calendar a first-class INPUT and a deterministic GUARD.
#
#   blackout_min      — no NEW entries within +/- this many minutes of a
#                       qualifying release (the tape around CPI/FOMC is noise,
#                       and a bracket placed into it gets whipsawed).
#   blackout_importance — 1 = only high-impact events (CPI, FOMC, NFP, PCE...).
#   event_day_size_cut  — size multiplier applied on a day carrying a qualifying
#                       event, so exposure is halved rather than zeroed.
#   block_overnight_into_fomc — never carry NEW risk through a same-day FOMC
#                       decision unless the position is already profitable.
# Everything here FAILS OPEN: if the calendar cannot be fetched, trading
# proceeds exactly as it did before (econ.py returns empty, not an error).
EVENT_GUARD = {
    "enabled": True,
    "countries": ("US",),
    "blackout_min": 30,
    "blackout_importance": 1,
    # Which events make a day an "event day" for the SIZE CUT. Calibrated to 1
    # (high only: CPI, FOMC, NFP, PCE, GDP, retail sales) on purpose. At 0 the
    # cut fired almost daily — a Fed speech or a mid-tier survey counts as
    # medium on the TradingView scale — which would have amounted to a permanent
    # 50% size reduction. That is the same flinch this whole release removed,
    # just dressed up as event awareness. Medium events are still listed in the
    # LLM's calendar digest for information.
    "event_day_importance": 1,
    "event_day_size_cut": 0.5,
    "block_overnight_into_fomc": True,
}

# --- self-improvement sample gate ---
# The routine's own first lesson (2026-09-01) was "do not tune any rule until at
# least 30 closed paper trades are logged". It then tuned three times at 5, 6 and
# 7 trades. This constant makes that gate a MECHANICAL constraint: below it, the
# routine may still write lessons and proposals, but it may NOT auto-apply a knob.
# A streak smaller than this is indistinguishable from variance, and tuning on it
# is how a system talks itself into a corner.
MIN_CLOSED_TRADES_TO_TUNE = 30

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
    "MR": MR_TUNE,
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
                               ("YOLO", YOLO_TUNE), ("MR", MR_TUNE)):
        section_tune = __TUNE__.get(section, {})
        over = raw.get(section)
        if not isinstance(over, dict):
            continue
        target = {"DAILY": DAILY, "WEEKLY": WEEKLY, "YOLO": YOLO, "MR": MR}[section]
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
            ("YOLO", YOLO, YOLO_TUNE),
            ("MR", MR, MR_TUNE)):
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
            ("YOLO", YOLO, YOLO_TUNE),
            ("MR", MR, MR_TUNE)):
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
            if sec not in ("DAILY", "WEEKLY", "YOLO", "MR"):
                continue
            target = {"DAILY": DAILY, "WEEKLY": WEEKLY, "YOLO": YOLO, "MR": MR}[sec]
            tune = {"DAILY": DAILY_TUNE, "WEEKLY": WEEKLY_TUNE, "YOLO": YOLO_TUNE,
                    "MR": MR_TUNE}[sec]
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
    for sec in ("DAILY", "WEEKLY", "YOLO", "MR"):
        for k in _IRREDUCIBLE:
            if isinstance(candidate.get(sec), dict) and k in candidate[sec]:
                notes.append(f"DROPPED irreducible floor knob {sec}.{k}")
    for k in _IRREDUCIBLE:  # flat floor keys, e.g. {'require_stop_loss': False}
        if k in candidate:
            notes.append(f"DROPPED irreducible floor knob {k}")
    clean = _tokenize(candidate)
    return clean, notes
