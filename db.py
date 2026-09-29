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

-- Every ORIGINAL post seen from a followed X account (x_copy_run.py), plus what
-- the bot made of it. This table is the monitoring feed the user asked for: it
-- is the record of "he posted X at T, and here is what the bot did about it".
--
-- post_id is the PRIMARY KEY because that is the only genuinely stable
-- identity: the feed re-reads a trailing window on every poll and every
-- provider returns the same numeric id, so a re-read is an upsert (no duplicate
-- signal) and a late-arriving post is still caught.
CREATE TABLE IF NOT EXISTS x_posts (
    post_id TEXT PRIMARY KEY,         -- numeric X post id, as a string
    author TEXT NOT NULL,             -- handle without '@'
    url TEXT,
    posted_at TEXT,                   -- when HE published it (ISO8601 UTC)
    text TEXT,
    is_reply INTEGER NOT NULL DEFAULT 0,
    has_media INTEGER NOT NULL DEFAULT 0,
    source TEXT,                      -- which feed provider returned it
    seen_at TEXT NOT NULL,            -- when WE first stored it
    state TEXT NOT NULL DEFAULT 'new',
        -- new       : stored, not yet read by the LLM
        -- ignored   : read; not a trade signal (commentary / poll / P&L post)
        -- review    : read; looks like a signal but we cannot act on it safely
        --             (no symbol, media-only, unverifiable ticker)
        -- open      : actionable ENTRY, waiting to execute (queued to the open)
        -- exit      : actionable EXIT for a symbol we hold
        -- done      : executed (or shadowed)
        -- rejected  : passed by the risk floor / gates
    kind TEXT,                        -- open | add | close | trim | none | unknown
    symbol TEXT,
    side TEXT,
    confidence REAL,
    reason TEXT,                      -- human-readable disposition, shown as-is
    analysis_json TEXT,               -- the full LLM read, for the audit trail
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_x_posts_posted ON x_posts(posted_at);

-- Small key/value store for feed bookkeeping (when the last gap-sweep ran, what
-- the last feed error was). A table of its own because these are facts about the
-- FEED, not about a post or an account, and inventing them from other tables
-- would be guesswork.
CREATE TABLE IF NOT EXISTS feed_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_x_posts_state ON x_posts(state);
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
    # Exit-quality telemetry (r_telemetry.py). Two of these are MEASURED after
    # the close (best/worst excursion inside the holding window, in R) and one is
    # recorded as of the entry (how wide the stop was, in ATR units). They exist
    # because "stop_out_share ~0.2 with avg_R -0.7" cannot distinguish an exit
    # rule that gives winners back from entries that never follow through — and
    # that distinction is what every review cycle kept asking for.
    for col, decl in (("mfe_r", "REAL"), ("mae_r", "REAL"),
                      ("stop_atr", "REAL"), ("telemetry_at", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
    # Which X post caused this trade (x_copy_run.py). Null for the other bots.
    # A column rather than a decision_json key so the dashboard and the feed view
    # can join a position back to the post that opened it in one query.
    if "signal_post_id" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN signal_post_id TEXT")
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


def closed_trades_needing_telemetry(account: str | None = None,
                                    retry_after_hours: float = 6.0,
                                    force: bool = False) -> list[sqlite3.Row]:
    """Closed trades whose exit telemetry is missing (r_telemetry.py).

    `telemetry_at` records the last ATTEMPT, not just success, so a row the feed
    cannot measure (a symbol outside the free 5-min history cap) is not re-probed
    on every 10-minute reconcile tick — it is retried after `retry_after_hours`.
    `force=True` ignores both the attempt stamp and the already-filled values.
    """
    conn = connect()
    try:
        q = "SELECT * FROM trades WHERE status!='open'"
        p: list = []
        if not force:
            q += " AND (mfe_r IS NULL OR stop_atr IS NULL)"
            if retry_after_hours:
                cut = (datetime.now(timezone.utc)
                       - timedelta(hours=float(retry_after_hours))).isoformat()
                q += " AND (telemetry_at IS NULL OR telemetry_at <= ?)"
                p.append(cut)
        if account:
            q += " AND account=?"
            p.append(account)
        q += " ORDER BY id"
        return conn.execute(q, p).fetchall()
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
            (symbol, since)).fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# X posts — the followed account's feed (x_copy_run.py)
# --------------------------------------------------------------------------- #
def upsert_x_post(post_id: str, author: str, url: str = "", posted_at: str = "",
                  text: str = "", is_reply: int = 0, has_media: int = 0,
                  source: str = "") -> bool:
    """Store a post we may not have seen before. Returns True if it is NEW.

    Idempotent by post_id: the feed deliberately re-reads a trailing window on
    every poll (so a post that only reaches the search index minutes later is
    still caught), which means the same post arrives over and over. A re-read
    must never look like a new signal, and it must never overwrite the
    disposition the bot already recorded for that post.
    """
    conn = connect()
    try:
        row = conn.execute("SELECT post_id FROM x_posts WHERE post_id=?",
                           (str(post_id),)).fetchone()
        if row:
            # Refresh only the immutable facts (a provider may fill in a blank
            # text or a media flag on a later pass); never touch state/analysis.
            conn.execute(
                "UPDATE x_posts SET posted_at=COALESCE(NULLIF(?,''), posted_at), "
                "text=CASE WHEN COALESCE(?,'')<>'' THEN ? ELSE text END, "
                "url=COALESCE(NULLIF(?,''), url), "
                "has_media=MAX(has_media, ?), is_reply=?, updated_at=? "
                "WHERE post_id=?",
                (posted_at, text, text, url, int(has_media), int(is_reply), _now(),
                 str(post_id)))
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO x_posts (post_id, author, url, posted_at, text, is_reply, "
            "has_media, source, seen_at, state, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'new',?)",
            (str(post_id), author, url, posted_at, text, int(is_reply),
             int(has_media), source, _now(), _now()))
        conn.commit()
        return True
    finally:
        conn.close()


def set_x_post(post_id: str, **fields) -> None:
    """Update a post's disposition (state/kind/symbol/reason/analysis_json)."""
    if not fields:
        return
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = connect()
    try:
        conn.execute(f"UPDATE x_posts SET {sets} WHERE post_id=?",
                     [*fields.values(), str(post_id)])
        conn.commit()
    finally:
        conn.close()


def x_post(post_id: str):
    conn = connect()
    try:
        return conn.execute("SELECT * FROM x_posts WHERE post_id=?",
                            (str(post_id),)).fetchone()
    finally:
        conn.close()


def x_posts(limit: int = 50, states: tuple | None = None) -> list:
    """Posts, newest first. `states` filters the disposition."""
    conn = connect()
    try:
        q = "SELECT * FROM x_posts"
        p: list = []
        if states:
            q += f" WHERE state IN ({','.join('?' * len(states))})"
            p.extend(states)
        q += " ORDER BY COALESCE(posted_at,'') DESC, post_id DESC LIMIT ?"
        p.append(int(limit))
        return conn.execute(q, p).fetchall()
    finally:
        conn.close()


def x_posts_newest_id() -> str | None:
    """The highest post id we have stored (post ids are monotonic)."""
    conn = connect()
    try:
        r = conn.execute("SELECT MAX(CAST(post_id AS INTEGER)) AS m FROM x_posts").fetchone()
        return str(r["m"]) if r and r["m"] is not None else None
    finally:
        conn.close()


def x_posts_last_seen() -> str | None:
    """Timestamp of the newest stored post (its publication time)."""
    conn = connect()
    try:
        r = conn.execute("SELECT MAX(posted_at) AS m FROM x_posts").fetchone()
        return r["m"] if r and r["m"] else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Feed bookkeeping (when the last gap-sweep ran, last error, ...)
# --------------------------------------------------------------------------- #
def feed_state_get(key: str, default: str | None = None) -> str | None:
    conn = connect()
    try:
        r = conn.execute("SELECT value FROM feed_state WHERE key=?", (str(key),)).fetchone()
        return r["value"] if r else default
    finally:
        conn.close()


def feed_state_set(key: str, value: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO feed_state (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (str(key), str(value), _now()))
        conn.commit()
    finally:
        conn.close()
