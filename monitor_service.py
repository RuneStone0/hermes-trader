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


def _age_minutes(ts: str | None) -> float | None:
    """Minutes since an ISO-8601 UTC timestamp (None if absent/unparseable)."""
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:                      # '2026-09-29 02:14:33.123+00:00'
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


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
    health: dict = {}
    try:
        health = json.loads(r.stdout or "{}")
        info["health"] = health.get("status", "unreachable")
        if health.get("status") != "ok":
            problems.append(f"health endpoint not ok: {(r.stdout or '')[:200]}")
    except Exception:
        problems.append(f"health endpoint unreachable: {(r.stdout or r.stderr or '')[:200]}")
        info["health"] = "unreachable"

    # 2b. The X-copy follower's feed. A bot that cannot read its source is a
    # silent failure of exactly the kind this watchdog exists for: it looks idle
    # while in fact it is blind. The feed reports the age of its last successful
    # read, so a stalled poller is visible here instead of only on the dashboard.
    xf = health.get("x_feed") or {}
    if xf and not xf.get("error"):
        last_poll = xf.get("last_poll") or xf.get("last_sweep")
        age_min = _age_minutes(last_poll)
        info["x_feed"] = (f"last check {age_min:.0f}m ago" if age_min is not None
                          else "never polled")
        if age_min is None:
            problems.append("X feed has never completed a poll")
        elif age_min > 30:
            problems.append(f"X feed has not polled in {age_min:.0f} min "
                            f"(expected every 10): the follower is blind")
        if xf.get("last_error"):
            err_age = _age_minutes(xf.get("last_error_at"))
            if err_age is None or err_age <= 90:
                problems.append(f"X feed error: {str(xf['last_error'])[:160]}")
        if not xf.get("account_live"):
            # Not a fault: shadow mode is the designed state until a fourth paper
            # account exists. Recorded so the status file never implies real
            # positions that do not exist.
            info["copy_mode"] = "shadow (no broker account; nothing is traded)"
    elif xf.get("error"):
        problems.append(f"X feed status unavailable: {str(xf['error'])[:160]}")

    # 3. recent errors in container logs
    r = _ssh("docker logs --since 30m hermes-trader 2>&1 | grep -iE 'error|timeout|traceback|exception' | tail -5")
    errs = (r.stdout or "").strip()
    info["recent_errors"] = errs or "none"
    if errs:
        problems.append("recent error lines in container logs")

    return problems, info


def main() -> None:
    problems, info = check()

    # Auto-recover: if the container is missing/unhealthy, try to bring it back.
    recovery = ""
    if "healthy" not in str(info.get("container", "")):
        r = _ssh("cd /home/umbrel/trader && docker compose up -d 2>&1 | tail -2")
        recovery = (r.stdout or r.stderr or "").strip() or "attempted"
        problems, info = check()  # re-check after recovery

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Service status — {now}", "",
             f"- container: {info['container']}",
             f"- health: {info['health']}"]
    if info.get("x_feed"):
        lines.append(f"- X feed: {info['x_feed']}")
    if info.get("copy_mode"):
        lines.append(f"- copy bot: {info['copy_mode']}")
    if recovery:
        lines.append(f"- auto-recovery: {recovery}")
    if info.get("recent_errors") and info["recent_errors"] != "none":
        lines.append(f"\nRecent log errors:\n```\n{info['recent_errors']}\n```")
    STATUS_PATH.write_text("\n".join(lines) + "\n")

    if problems:
        print("HERMES-TRADER SERVICE ALERT")
        if recovery:
            print(f"  - auto-recovery ran: {recovery}")
        for p in problems:
            print(f"  - {p}")
        if info.get("recent_errors") and info["recent_errors"] != "none":
            print("  recent log errors:")
            print("    " + info["recent_errors"].replace("\n", "\n    "))
    # else: silent (healthy)


if __name__ == "__main__":
    main()
