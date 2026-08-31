"""Service monitor — checks the hermes-trader container health over SSH.

Watchdog pattern: prints NOTHING when healthy (silent tick), prints an ALERT
when something is wrong. Always writes a human-readable status file for
at-a-glance review.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HOST = "umbrel@10.21.0.1"
KEY = "/opt/data/home/.ssh/id_ed25519"
STATUS_PATH = Path("/opt/data/profiles/trader/trading/data/service_status.md")


def _ssh(cmd: str):
    return subprocess.run(
        ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=10", HOST, cmd],
        capture_output=True, text=True, timeout=30,
    )


def check() -> tuple[list[str], dict]:
    problems: list[str] = []
    info: dict = {}

    # 1. container running + healthy
    r = _ssh("docker ps --filter name=hermes-trader --format '{{.Status}}'")
    status_line = (r.stdout or "").strip()
    if r.returncode != 0:
        problems.append(f"ssh/docker failed: {(r.stderr or '')[:200]}")
        info["container"] = "UNKNOWN (ssh error)"
    else:
        info["container"] = status_line or "NOT RUNNING"
        if "healthy" not in status_line:
            problems.append(f"container unhealthy/not running: {status_line or '(no output)'}")

    # 2. health endpoint
    r = _ssh("curl -s -m 5 localhost:43210/health")
    try:
        h = json.loads(r.stdout or "{}")
        info["health"] = h.get("status", "unreachable")
        if h.get("status") != "ok":
            problems.append(f"health endpoint not ok: {(r.stdout or '')[:200]}")
    except Exception:
        problems.append(f"health endpoint unreachable: {(r.stdout or r.stderr or '')[:200]}")
        info["health"] = "unreachable"

    # 3. recent errors in container logs
    r = _ssh("docker logs --since 30m hermes-trader 2>&1 | grep -iE 'error|timeout|traceback|exception' | tail -5")
    errs = (r.stdout or "").strip()
    info["recent_errors"] = errs or "none"
    if errs:
        problems.append("recent error lines in container logs")

    return problems, info


def main() -> None:
    problems, info = check()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Service status — {now}", "",
             f"- container: {info['container']}",
             f"- health: {info['health']}"]
    if info.get("recent_errors") and info["recent_errors"] != "none":
        lines.append(f"\nRecent log errors:\n```\n{info['recent_errors']}\n```")
    STATUS_PATH.write_text("\n".join(lines) + "\n")

    if problems:
        print("HERMES-TRADER SERVICE ALERT")
        for p in problems:
            print(f"  - {p}")
        if info.get("recent_errors") and info["recent_errors"] != "none":
            print("  recent log errors:")
            print("    " + info["recent_errors"].replace("\n", "\n    "))
    # else: silent (healthy)


if __name__ == "__main__":
    main()
