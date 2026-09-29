"""Container entrypoint: HTTP dashboard/health server + job scheduler.

Runs the strategies on schedules that mirror the original Hermes cron jobs (UTC),
serves the dark-theme dashboard and a JSON health endpoint, and writes per-job
logs under $TRADER_HOME/logs. Standard library only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import db
from alpaca_rest import AlpacaClient

PORT = int(os.environ.get("PORT", "43210"))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
START = time.time()

# name -> schedule + command + timeout. All times are UTC, Mon-Fri (market days).
# These mirror the original Hermes cron definitions.
JOBS = [
    {"name": "daily", "type": "window", "start": "13:00", "end": "21:00",
     "interval_s": 300, "cmd": [sys.executable, "daily_run.py"], "timeout": 180},
    {"name": "weekly", "type": "times", "times": ["14:45"],
     "cmd": [sys.executable, "weekly_run.py"], "timeout": 180},
    {"name": "reconcile", "type": "interval", "interval_s": 600,
     "cmd": [sys.executable, "reconcile.py"], "timeout": 180},
    {"name": "selfheal", "type": "interval", "interval_s": 600,
     "cmd": [sys.executable, "selfheal.py"], "timeout": 180},
    {"name": "yolo", "type": "window", "start": "13:00", "end": "21:00", "interval_s": 1800,
     "market_gate": True, "cmd": [sys.executable, "yolo_run.py"], "timeout": 300},
    {"name": "self_improve", "type": "times", "times": ["21:30"],
     "cmd": [sys.executable, "self_improve.py"], "timeout": 300},
    # The X-copy follower (x_copy_run.py). Deliberately NOT gated on the market
    # clock or on weekdays: the whole point is to see his post the moment it
    # appears, which can be any hour of any day. An entry that arrives while the
    # market is shut waits for the open inside the bot itself, so nothing is
    # chased at a bad price just because the poller is always awake.
    {"name": "x_feed", "type": "interval", "interval_s": 600,
     "cmd": [sys.executable, "x_copy_run.py"], "timeout": 240},
    # Mean-reversion sleeve on the daily account (mr_run.py). Its entries are
    # close-based, so most ticks only manage exits — the job is cheap and the
    # 15-min cadence keeps the rule exit responsive.
    {"name": "mr", "type": "window", "start": "13:00", "end": "21:00", "interval_s": 900,
     "market_gate": True, "cmd": [sys.executable, "mr_run.py"], "timeout": 240},
    # Warm the economic-calendar cache BEFORE the session so the first decision
    # of the day never pays a network round-trip (and so a feed outage shows up
    # in the logs while the market is still closed). Twice a day keeps the 7-day
    # forward window continuous even if a refresh fails.
    {"name": "econ", "type": "times", "times": ["12:45", "20:45"],
     "cmd": [sys.executable, "-c",
             "import econ; r=econ.refresh(force=True); "
             "print(r['source'], len(r['events']), 'events'); print(econ.brief()[:400])"],
     "timeout": 120},
]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


_market_clock = AlpacaClient("yolo")
_market_cache: dict = {"ts": 0.0, "open": False}


def _market_open() -> bool:
    """Cached Alpaca market-open check (at most one API call per minute)."""
    now = time.time()
    if now - _market_cache["ts"] >= 60:
        try:
            _market_cache["open"] = bool(_market_clock.clock().get("is_open"))
        except Exception:
            _market_cache["open"] = False
        _market_cache["ts"] = now
    return _market_cache["open"]


def should_run(job: dict, now: datetime, last: datetime | None) -> bool:
    t = job["type"]
    if t == "interval":
        return last is None or (now - last).total_seconds() >= job["interval_s"]
    if now.weekday() >= 5:  # Sat/Sun — market closed
        return False
    if t == "times":
        if _hm(now) not in job["times"]:
            return False
        # Guard against double-firing within the same clock minute (loop ticks
        # every 20s). Compare elapsed time, NOT minute-of-day strings: the old
        # `_hm(last) != _hm(now)` check suppressed the next weekday's run
        # entirely whenever the previous run happened at the same minute.
        return last is None or (now - last).total_seconds() >= 60
    if t == "window":
        if not (job["start"] <= _hm(now) < job["end"]):
            return False
        if last is None or (now - last).total_seconds() >= job["interval_s"]:
            if job.get("market_gate") and not _market_open():
                return False
            return True
        return False
    return False


def _log(name: str, text: str) -> None:
    logdir = config.PROFILE_HOME / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    with open(logdir / f"{name}.log", "a") as f:
        f.write(text)


def _txt(x) -> str:
    """Coerce TimeoutExpired stdout/stderr to str.

    CPython's Popen._check_timeout joins the raw pipe chunks, so even with
    text=True the captured partial output is BYTES. Concatenating it to the
    f-string raised TypeError inside the handler, which killed the reporting
    branch: a timed-out job wrote NOTHING to its own log (2026-09-23: selfheal
    + reconcile timed out twice, only a bare scheduler traceback survived).
    """
    if x is None:
        return ""
    return x if isinstance(x, str) else x.decode("utf-8", errors="replace")


def run_job(job: dict) -> None:
    name = job["name"]
    t0 = time.time()
    try:
        proc = subprocess.run(job["cmd"], cwd=APP_DIR, capture_output=True,
                              text=True, timeout=job.get("timeout", 180))
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = (f"TIMEOUT after {job.get('timeout', 180)}s "
               f"(partial output captured before the kill)\n"
               + _txt(e.stdout) + _txt(e.stderr))
        rc = -1
    except Exception:
        out = traceback.format_exc()
        rc = -2

    ts = _now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
    _log(name, f"\n=== {ts} rc={rc} ({time.time() - t0:.1f}s) ===\n{out}\n")


def scheduler_loop() -> None:
    last: dict[str, datetime | None] = {j["name"]: None for j in JOBS}
    while True:
        now = _now_utc()
        for j in JOBS:
            try:
                if should_run(j, now, last[j["name"]]):
                    last[j["name"]] = now
                    run_job(j)
            except Exception:
                _log("scheduler", traceback.format_exc())
        time.sleep(20)


def _health() -> dict:
    try:
        db.init_db()
        n_open = len(db.open_trades())
        n_closed = len(db.all_trades()) - n_open
    except Exception as e:
        return {"status": "degraded", "error": str(e)}
    health = {
        "status": "ok",
        "uptime_s": int(time.time() - START),
        "open_positions": n_open,
        "closed_trades": n_closed,
        "server_time": _now_utc().isoformat(timespec="seconds"),
    }
    # Feed liveness for the copy follower. Additive on purpose: the existing
    # watchdog reads `status`, and a bot that cannot see its source is exactly
    # the kind of quiet failure that deserves to be visible here.
    try:
        posts = db.x_posts(limit=1)
        health["x_feed"] = {
            "handle": config.X_FEED["handle"],
            "last_post_at": posts[0]["posted_at"] if posts else None,
            "posts_stored": len(db.x_posts(limit=1000)),
            "last_poll": db.feed_state_get("last_poll"),
            "last_sweep": db.feed_state_get("last_sweep"),
            "last_error": db.feed_state_get("last_error"),
            "last_error_at": db.feed_state_get("last_error_at"),
            "account_live": config.copy_account_configured(),
        }
    except Exception as e:                                         # noqa: BLE001
        health["x_feed"] = {"error": str(e)}
    return health


def _signals(limit: int = 25) -> dict:
    """Recent posts by the followed account + what the bot did about them.

    Read-only, and the same records the dashboard renders: this exists so the
    Hermes-side watcher can report a new post (and what the bot did with it)
    without scraping HTML.
    """
    try:
        db.init_db()
        rows = db.x_posts(limit=int(limit))
    except Exception as e:                                         # noqa: BLE001
        return {"error": str(e), "posts": []}
    return {
        "handle": config.X_FEED["handle"],
        "generated_at": _now_utc().isoformat(timespec="seconds"),
        # One nested block, the same keys /health exposes, so the two consumers
        # (the SSH watchdog and the post watcher) can never drift apart.
        "feed": {
            "last_poll": db.feed_state_get("last_poll"),
            "last_sweep": db.feed_state_get("last_sweep"),
            "last_error": db.feed_state_get("last_error"),
            "last_error_at": db.feed_state_get("last_error_at"),
            "account_live": config.copy_account_configured(),
        },
        "posts": [{
            "post_id": r["post_id"],
            "posted_at": r["posted_at"],
            "url": r["url"],
            "text": r["text"],
            "state": r["state"],
            "kind": r["kind"],
            "symbol": r["symbol"],
            "side": r["side"],
            "confidence": r["confidence"],
            "reason": r["reason"],
        } for r in rows],
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/health"):
            self._send(200, "application/json", json.dumps(_health()).encode())
            return
        if self.path.startswith("/signals"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                limit = max(1, min(int(q.get("limit", ["25"])[0]), 200))
            except (TypeError, ValueError):
                limit = 25
            self._send(200, "application/json", json.dumps(_signals(limit)).encode())
            return
        try:
            import dashboard
            account = self.path.strip("/").split("?")[0]
            if account in ("daily", "weekly", "yolo", "copy"):
                html = dashboard.build_account(account).encode()
            else:
                html = dashboard.build().encode()
        except Exception as e:
            html = f"dashboard error: {e}".encode()
        self._send(200, "text/html; charset=utf-8", html)

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    db.init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[app] serving dashboard + health on :{PORT}; scheduler running {len(JOBS)} jobs")
    srv.serve_forever()


if __name__ == "__main__":
    main()
