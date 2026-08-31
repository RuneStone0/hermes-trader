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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account);
CREATE INDEX IF NOT EXISTS idx_trades_status  ON trades(status);
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
        conn.commit()
    finally:
        conn.close()


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
