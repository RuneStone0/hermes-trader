"""SQLite persistence for all trades across all three accounts.

Schema: one row per trade (open then closed in place). The dashboard and the
nightly self-improvement routine both read from here.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

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


def insert_trade(**fields) -> int:
    fields.setdefault("created_at", _now())
    fields.setdefault("updated_at", _now())
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


def recent_events(limit: int = 50, account: str | None = None) -> list[sqlite3.Row]:
    conn = connect()
    try:
        if account:
            return conn.execute(
                "SELECT * FROM events WHERE account=? ORDER BY id DESC LIMIT ?",
                (account, int(limit)),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
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
