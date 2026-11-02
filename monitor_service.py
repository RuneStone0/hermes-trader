"""Service monitor — checks the hermes-trader container health over SSH.

Watchdog pattern: prints NOTHING when healthy (silent tick), prints an ALERT
when something is wrong. Always writes a human-readable status file for
at-a-glance review.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOST = "umbrel@10.21.0.1"
KEY = "/opt/data/home/.ssh/id_ed25519"
STATUS_PATH = Path("/opt/data/profiles/trader/trading/data/service_status.md")

# A client hanging up is not an error. socketserver wraps every dropped connection in a full
# "Traceback ... ConnectionResetError" block; its header line is byte-identical to a real crash,
# so the block has to be judged as a whole rather than filtered line by line (learned 2026-11-02,
# when the monitor alerted on a service that was up 890s with status ok).
_NOISE = (
    "ConnectionResetError",
    "Connection reset by peer",
    "BrokenPipeError",
    "ConnectionAbortedError",
    "RemoteDisconnected",
    "Exception occurred during processing of request",
)
_ERRWORDS = re.compile(r"error|timeout|traceback|exception", re.I)


def _real_errors(log_text: str, limit: int = 5) -> str:
    """Return genuine error lines, dropping tracebacks that are nothing but client disconnects."""
    lines = (log_text or "").splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Traceback (most recent call last)"):
            block = [line]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.startswith((" ", "\t")) or re.match(
                    r"^[A-Za-z_.]*(Error|Exception|Interrupt|Warning)\b", nxt
                ):
                    block.append(nxt)
                    i += 1
                    continue
                break
            body = "\n".join(block)
            if any(n in body for n in _NOISE):
                continue                      # caller hung up — not a fault
            kept.extend(block[-3:])           # keep the tail of a genuine traceback
            continue
        if _ERRWORDS.search(line) and not any(n in line for n in _NOISE):
            kept.append(line)
        i += 1
    return "\n".join(kept[-limit:])


def _ssh(cmd: str):
    return subprocess.run(
        ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
         "-o", "PasswordAuthentication=no", HOST, cmd],
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

    # 2. health endpoint — asked directly over HTTP, never through ssh.
    #    An ssh failure used to coerce to "{}", which then read as
    #    "status is not ok" with a blank reason: a transport outage disguised
    #    as a dead service. A transport failure must say so in its own words.
    try:
        with urllib.request.urlopen("http://10.21.0.1:43210/health", timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
        health: dict = json.loads(raw)
        info["health"] = health.get("status", "unknown")
        if health.get("status") != "ok":
            problems.append(f"health endpoint not ok: {raw[:200]}")
    except Exception as exc:
        problems.append(
            f"health endpoint UNREACHABLE (transport, not health): {type(exc).__name__}: {exc}"
        )
        info["health"] = "unreachable"

    # (the X-copy follower's feed-health block was removed with it on 2026-10-15)

    # 3. recent errors in container logs
    #
    # Pull the tail and judge it in Python rather than grepping for "error": a disconnect
    # traceback's header is identical to a real crash, so only a whole-block test separates them
    # (see _real_errors). Real timeouts and application exceptions still surface.
    r = _ssh("docker logs --since 30m hermes-trader 2>&1 | tail -400")
    errs = _real_errors(r.stdout or "")
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
