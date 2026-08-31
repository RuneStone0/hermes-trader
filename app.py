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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import db

PORT = int(os.environ.get("PORT", "8080"))
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
    {"name": "yolo", "type": "times", "times": ["14:00", "17:00", "19:00"],
     "cmd": [sys.executable, "yolo_run.py"], "timeout": 300},
    {"name": "self_improve", "type": "times", "times": ["21:30"],
     "cmd": [sys.executable, "self_improve.py"], "timeout": 300},
]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def should_run(job: dict, now: datetime, last: datetime | None) -> bool:
    t = job["type"]
    if t == "interval":
        return last is None or (now - last).total_seconds() >= job["interval_s"]
    if now.weekday() >= 5:  # Sat/Sun — market closed
        return False
    if t == "times":
        if _hm(now) not in job["times"]:
            return False
        return last is None or _hm(last) != _hm(now)
    if t == "window":
        if not (job["start"] <= _hm(now) < job["end"]):
            return False
        return last is None or (now - last).total_seconds() >= job["interval_s"]
    return False


def _log(name: str, text: str) -> None:
    logdir = config.PROFILE_HOME / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    with open(logdir / f"{name}.log", "a") as f:
        f.write(text)


def run_job(job: dict) -> None:
    name = job["name"]
    t0 = time.time()
    try:
        proc = subprocess.run(job["cmd"], cwd=APP_DIR, capture_output=True,
                              text=True, timeout=job.get("timeout", 180))
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = f"TIMEOUT after {job.get('timeout', 180)}s\n" + (e.stdout or "") + (e.stderr or "")
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
    return {
        "status": "ok",
        "uptime_s": int(time.time() - START),
        "open_positions": n_open,
        "closed_trades": n_closed,
        "server_time": _now_utc().isoformat(timespec="seconds"),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, "application/json", json.dumps(_health()).encode())
            return
        # Everything else -> dashboard, regenerated fresh on each view.
        try:
            import dashboard
            dashboard.main()
        except Exception:
            pass
        html = config.DASHBOARD_PATH.read_bytes() if config.DASHBOARD_PATH.exists() else b"no data yet"
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
