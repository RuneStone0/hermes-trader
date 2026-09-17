"""SQLite persistence for all trades across all three accounts.

Schema: one row per trade (open then closed in place). The dashboard and the
nightly self-improvement routine both read from here.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,            -- daily | weekly | yolo
    strategy TEXT NOT NULL,           -- e.g. 'daily_orb', 'weekly_pullback', 'yolo'
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,        -- equity | etf | option
    side TEXT NOT NULL,               -- long | short
    qty REAL NOT NULL,                -- shares (or contracts for options)
    entry_price REAL,                 -- avg entry (null until filled)
    exit_price REAL,                  -- avg exit (null while open)
    entry_time TEXT,
    exit_time TEXT,
    status TEXT NOT NULL,             -- open | closed
    gross_pnl REAL,                   -- realized gross P/L (null while open)
    fees REAL NOT NULL DEFAULT 0,     -- modelled regulatory fees
    net_pnl REAL,                     -- gross_pnl - fees
    rr_planned REAL,                  -- planned reward:risk at entry
    stop_price REAL,
    target_price REAL,
    last_price REAL,                  -- latest mark for an open position (reconcile)
    unrealized_pl REAL,               -- open position unrealized P/L (reconcile)
    close_reason TEXT,                -- why it closed: stop|target|market|other
    order_id TEXT,
    client_order_id TEXT,
    note TEXT,
    decision_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account);
CREATE INDEX IF NOT EXISTS idx_trades_status  ON trades(status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,            -- daily | weekly | yolo
    strategy TEXT NOT NULL,           -- daily_orb | weekly_pullback | yolo
    decision TEXT NOT NULL,           -- go | no_go | skip | exit | error
    reason TEXT,                      -- human-readable "why"
    detail TEXT,                      -- optional extra (symbol/qty/prices/rationale)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS account_state (
    account TEXT PRIMARY KEY,
    equity REAL,
    cash REAL,
    last_equity REAL,
    starting_equity REAL,
    updated_at TEXT NOT NULL
);

-- Equity time series, written by reconcile (~10 min cadence). This is what makes
-- drawdown measurable at all: account_state keeps only the LATEST snapshot, so
-- without a history there is no high-water mark to measure a drawdown from —
-- and risk_gov.py's de-risking, the dashboard's benchmark comparison and the
-- nightly review all depend on one. Deliberately tiny rows: (account, minute,
-- equity, cash). Old rows are pruned to KEEP_DAYS on each write.
CREATE TABLE IF NOT EXISTS equity_history (
    account TEXT NOT NULL,
    ts TEXT NOT NULL,                 -- ISO8601 UTC, truncated to the minute
    equity REAL,
    cash REAL,
    PRIMARY KEY (account, ts)
);
CREATE INDEX IF NOT EXISTS idx_equity_hist_account ON equity_history(account, ts);

-- Daily SPY closes, so every account page can plot buy-and-hold against the
-- bot's own equity curve. "Did we make money" is the wrong question; the bots
-- were measured against zero, which flatters a flat tape.
CREATE TABLE IF NOT EXISTS benchmark_history (
    symbol TEXT NOT NULL,
    d TEXT NOT NULL,                  -- YYYY-MM-DD
    close REAL,
    PRIMARY KEY (symbol, d)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema (safe on existing DBs)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "decision_json" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN decision_json TEXT")
    if "last_price" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN last_price REAL")
    if "unrealized_pl" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN unrealized_pl REAL")
    if "close_reason" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN close_reason TEXT")
    st_cols = {r["name"] for r in conn.execute("PRAGMA table_info(account_state)").fetchall()}
    if "cash" not in st_cols:
        conn.execute("ALTER TABLE account_state ADD COLUMN cash REAL")
    if "last_equity" not in st_cols:
        conn.execute("ALTER TABLE account_state ADD COLUMN last_equity REAL")
    if "starting_equity" not in st_cols:
        conn.execute("ALTER TABLE account_state ADD COLUMN starting_equity REAL")
    # Backfill entry_time for trades written before insert_trade stamped it:
    # created_at IS the open timestamp, so it is the honest value to use.
    conn.execute("UPDATE trades SET entry_time=created_at "
                 "WHERE entry_time IS NULL AND status='open'")


def insert_trade(**fields) -> int:
    fields.setdefault("created_at", _now())
    fields.setdefault("updated_at", _now())
    # entry_time was never populated by any of the three run scripts, so every
    # trade in the journal had a null hold time (dashboard showed no duration,
    # and reconcile's fill-matching fell back to created_at). Stamp it here from
    # created_at so ALL callers get it for free.
    if fields.get("status") == "open":
        fields.setdefault("entry_time", fields["created_at"])
    cols = list(fields.keys())
    sql = f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    conn = connect()
    try:
        cur = conn.execute(sql, [fields[c] for c in cols])
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_trade(trade_id: int, **fields) -> None:
    fields["updated_at"] = _now()
    sets = ",".join(f"{k}=?" for k in fields)
    conn = connect()
    try:
        conn.execute(f"UPDATE trades SET {sets} WHERE id=?", [*fields.values(), trade_id])
        conn.commit()
    finally:
        conn.close()


def open_trades(account: str | None = None) -> list[sqlite3.Row]:
    conn = connect()
    try:
        if account:
            return conn.execute(
                "SELECT * FROM trades WHERE status='open' AND account=? ORDER BY id", (account,)
            ).fetchall()
        return conn.execute("SELECT * FROM trades WHERE status='open' ORDER BY id").fetchall()
    finally:
        conn.close()


def all_trades(account: str | None = None) -> list[sqlite3.Row]:
    conn = connect()
    try:
        if account:
            return conn.execute(
                "SELECT * FROM trades WHERE account=? ORDER BY id", (account,)
            ).fetchall()
        return conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
    finally:
        conn.close()


def log_event(account: str, strategy: str, decision: str, reason: str = "",
              detail: str | None = None, dedup: bool = False) -> int:
    """Record a bot decision/activity for the dashboard's decision log.

    dedup=True refreshes the timestamp of the last identical (account, strategy,
    decision, reason) event instead of inserting a duplicate — for no-op/waiting
    states that would otherwise spam the log every tick.
    """
    conn = connect()
    try:
        if dedup:
            row = conn.execute(
                "SELECT id FROM events WHERE account=? AND strategy=? AND decision=? "
                "AND COALESCE(reason,'')=? ORDER BY id DESC LIMIT 1",
                (account, strategy, decision, reason or ""),
            ).fetchone()
            if row:
                conn.execute("UPDATE events SET created_at=? WHERE id=?", (_now(), row["id"]))
                conn.commit()
                return row["id"]
        cur = conn.execute(
            "INSERT INTO events (account,strategy,decision,reason,detail,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (account, strategy, decision, reason or "", detail, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def recent_events(limit: int = 50, account: str | None = None,
                  strategy: str | None = None) -> list[sqlite3.Row]:
    conn = connect()
    try:
        q = "SELECT * FROM events WHERE 1=1"
        p: list = []
        if account:
            q += " AND account=?"
            p.append(account)
        if strategy:
            q += " AND strategy=?"
            p.append(strategy)
        q += " ORDER BY id DESC LIMIT ?"
        p.append(int(limit))
        return conn.execute(q, p).fetchall()
    finally:
        conn.close()


def last_event(account: str, strategy: str) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM events WHERE account=? AND strategy=? ORDER BY id DESC LIMIT 1",
            (account, strategy),
        ).fetchone()
    finally:
        conn.close()


def save_account_state(account: str, equity: float | None = None,
                       cash: float | None = None, last_equity: float | None = None,
                       starting_equity: float | None = None) -> None:
    """Upsert a broker snapshot (equity / cash / last_equity) for an account.

    Written by reconcile (every 10 min) so the dashboard can express position
    size / cash / % change without hitting the broker per page view. Uses an
    upsert so callers may snapshot a subset of fields without nulling others.
    `starting_equity` is SET ONCE — the first non-null value wins and is never
    overwritten (it's a lifetime baseline, not a moving snapshot).
    """
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO account_state (account, equity, cash, last_equity, starting_equity, "
            "updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(account) DO UPDATE SET "
            "equity=COALESCE(excluded.equity, account_state.equity), "
            "cash=COALESCE(excluded.cash, account_state.cash), "
            "last_equity=COALESCE(excluded.last_equity, account_state.last_equity), "
            "starting_equity=COALESCE(account_state.starting_equity, excluded.starting_equity), "
            "updated_at=excluded.updated_at",
            (account, equity, cash, last_equity, starting_equity, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def account_state() -> dict[str, dict]:
    """Latest known broker snapshot per account:
    {'daily': {'equity':..,'cash':..,'last_equity':..,'starting_equity':..}, ...}."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT account, equity, cash, last_equity, starting_equity "
            "FROM account_state").fetchall()
        return {r["account"]: {"equity": r["equity"], "cash": r["cash"],
                               "last_equity": r["last_equity"],
                               "starting_equity": r["starting_equity"]}
                for r in rows}
    finally:
        conn.close()


def account_equity() -> dict[str, float]:
    """Latest known equity per account: {'daily': 9994.69, ...}."""
    conn = connect()
    try:
        return {r["account"]: r["equity"] for r in conn.execute(
            "SELECT account, equity FROM account_state WHERE equity IS NOT NULL")}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Equity history (drawdown / benchmark / dashboard curves)
# --------------------------------------------------------------------------- #
EQUITY_KEEP_DAYS = 400


def record_equity(account: str, equity: float | None, cash: float | None = None) -> None:
    """Append an equity point (minute-truncated so a busy tick can't spam rows).

    Prunes rows older than EQUITY_KEEP_DAYS on the same call. Never raises on a
    null equity — a missing snapshot simply records nothing.
    """
    if equity is None:
        return
    ts = _now()[:16] + ":00+00:00"   # ISO8601 UTC truncated to the minute
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO equity_history (account, ts, equity, cash) VALUES (?,?,?,?) "
            "ON CONFLICT(account, ts) DO UPDATE SET equity=excluded.equity, cash=excluded.cash",
            (account, ts, float(equity), float(cash) if cash is not None else None),
        )
        conn.execute("DELETE FROM equity_history WHERE ts < ?",
                     ((datetime.now(timezone.utc) - timedelta(days=EQUITY_KEEP_DAYS)).isoformat(),))
        conn.commit()
    finally:
        conn.close()


def equity_series(account: str, days: int = 60) -> list[dict]:
    """Ascending equity points for `account` over the last `days`."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT ts, equity, cash FROM equity_history "
            "WHERE account=? AND ts>=? AND equity IS NOT NULL ORDER BY ts",
            (account, since))]
    finally:
        conn.close()


def equity_peak(account: str) -> float | None:
    """Max recorded equity for `account` (the high-water mark)."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT MAX(equity) AS peak FROM equity_history WHERE account=?", (account,)
        ).fetchone()
        return float(row["peak"]) if row and row["peak"] is not None else None
    finally:
        conn.close()


def realized_pnl_today(account: str) -> float | None:
    """Sum of net_pnl for trades CLOSED today in US/Eastern (the trading day).

    Used as the conservative fallback for the daily-loss gate when the broker's
    last_equity snapshot is unavailable. Returns None when nothing closed today.
    """
    et = timezone(timedelta(hours=-4))  # EDT; the date boundary only needs ballpark
    today = datetime.now(et).strftime("%Y-%m-%d")
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT net_pnl, exit_time, updated_at FROM trades "
            "WHERE account=? AND status='closed'", (account,)
        ).fetchall()
    finally:
        conn.close()
    total = 0.0
    seen = False
    for r in rows:
        stamp = r["exit_time"] or r["updated_at"] or ""
        if str(stamp)[:10] == today:
            total += float(r["net_pnl"] or 0.0)
            seen = True
    return total if seen else None


def save_benchmark(symbol: str, points: list[tuple[str, float]]) -> None:
    """Upsert daily closes for a benchmark symbol (SPY buy-and-hold)."""
    if not points:
        return
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO benchmark_history (symbol, d, close) VALUES (?,?,?) "
            "ON CONFLICT(symbol, d) DO UPDATE SET close=excluded.close",
            [(symbol, d, float(c)) for d, c in points],
        )
        conn.commit()
    finally:
        conn.close()


def benchmark_series(symbol: str, days: int = 120) -> list[dict]:
    """Ascending daily closes for a benchmark symbol over the last `days`."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT d, close FROM benchmark_history WHERE symbol=? AND d>=? ORDER BY d",
            (symbol, since))]
    finally:
        conn.close()
