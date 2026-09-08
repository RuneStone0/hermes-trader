"""Trader profile — central config for the Alpaca trading bot system.

One place for every tunable. Strategy params are DATA (knobs), not logic: the
nightly self-improvement routine may PROPOSE changes (written to
strategy_proposals.md) but never auto-applies strategy changes.
"""
from __future__ import annotations

import os
from pathlib import Path

# App version (bumped manually on releases; shown in the dashboard footer).
VERSION = "1.0.0"

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
# Tradable universe (hard rule, applies to ALL traders)
# --------------------------------------------------------------------------- #
# Retail-accessible instruments only. NO crypto except Bitcoin.
CRYPTO_ALLOWLIST = ["BTC/USD"]


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
RISK_PCT_PER_TRADE = 0.01          # risk 1% of equity per trade
MAX_DAILY_LOSS_PCT = 0.015         # stop trading for the day after -1.5% realized
REWARD_RISK_TARGET = 1.5           # preferred reward:risk (NOT a hard requirement)
MIN_NET_RR = 1.0                   # profitability floor: net reward >= net risk after fees

# US market timezone (Alpaca clocks are in this tz)
MARKET_TZ = "America/New_York"

# --------------------------------------------------------------------------- #
# Daily strategy (Opening Range Breakout on SPY)
# --------------------------------------------------------------------------- #
DAILY = {
    "symbol": "SPY",
    "orb_start": "09:30",     # opening-range window (ET)
    "orb_end": "10:00",
    "rr_multiple": 1.5,       # target = entry +/- 1.5 * opening range (attainable, profitable)
    "close_confirmation": True,  # require a CLOSE beyond the range, not just a wick
    "flat_by": "15:50",       # force flat before the close
    "max_trades_per_day": 1,
}

# --------------------------------------------------------------------------- #
# Weekly strategy (trend-following pullback on SPY)
# --------------------------------------------------------------------------- #
WEEKLY = {
    "symbol": "SPY",
    "trend_sma_weeks": 20,
    "atr_period": 14,
    "atr_timeframe": "1Day",
    "stop_atr": 1.0,
    "target_atr": 1.5,
    "max_hold_days": 5,
    "max_positions": 1,
}

# --------------------------------------------------------------------------- #
# YOLO / "NoRulesRules" — full autonomy config
# --------------------------------------------------------------------------- #
# SAFETY_FLOOR keeps a minimal risk floor even for the fully-autonomous trader.
# Set False for a truly unconstrained bot (NOT recommended, even on paper).
YOLO = {
    "safety_floor": True,
    "max_position_pct": 0.25,        # max notional in one position (% of equity)
    "max_risk_pct": 0.02,            # max entry->stop risk per trade (% of equity)
    "max_concurrent_positions": 5,
    "require_stop_loss": True,
    "allowed_assets": ["us_equity", "us_etf", "us_option"],  # retail-accessible only
    "crypto_allowlist": CRYPTO_ALLOWLIST,  # crypto: BTC only (no other crypto)
    # Bracket-geometry buffers: all stops/targets are validated against the LIVE
    # price (Alpaca 422s any stop within $0.01 of it). These are the minimum
    # entry->stop / entry->target distances as a % of the live price.
    "min_stop_dist_pct": 0.002,     # stop must clear entry by >= 0.2%
    "min_target_dist_pct": 0.001,   # target must clear entry by >= 0.1%
    # v1 auto-trade universe (liquid, retail-accessible US equities/ETFs).
    # Options and BTC/USD are excluded from v1 auto-execution because they need
    # the options-chain / crypto market-data endpoints, which are follow-ups.
    "watchlist": [
        "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "IEF", "HYG",
        "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "SMH",
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    ],
}

# --------------------------------------------------------------------------- #
# Self-healing (OPERATIONAL knobs — NOT strategy; safe for autonomous runs)
# --------------------------------------------------------------------------- #
# selfheal.py restores intended broker state when runtime anomalies break it
# (day-TIF bracket legs cancelled at 20:00 ET leaving a naked position, orphaned
# protective orders on flat symbols, DB drift vs broker). It never opens NEW
# trades and never invents risk levels: protective prices are copied verbatim
# from the trade's own stop_price/target_price. Reconcile (fills/P&L) is
# separate; selfheal fixes STATE. Hermes monitors the outcome.
SELFHEAL = {
    "enabled": True,
    "adopt_missing_rows": True,    # R1: create DB row for a broker position missing one
    "reattach_protection": True,   # R2: re-place stop(+target) legs for naked positions
    "cancel_debris": True,         # R3: cancel orphan protective orders on flat symbols
    "void_unfilled": True,         # R4: void DB rows whose broker order cancelled unfilled
    "open_tif": "day",             # TIF for re-attached protection during market hours
    "after_hours_tif": "gtc",      # TIF after the close (protects the overnight hold)
}
