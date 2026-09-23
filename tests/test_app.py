"""Tests for the scheduler's job runner — the timeout reporting path.

Run: TRADER_HOME=/opt/data/profiles/trader python3 tests/test_app.py

Plain asserts, no pytest (the container has no third-party packages).

Incident this pins (2026-09-23, ~07:33-07:49 UTC): selfheal.py and reconcile.py
each blew the 180s job timeout on two consecutive ticks. The handler then did
`f"...TIMEOUT..." + (e.stdout or "")` — and CPython hands back the captured
partial output as BYTES even with text=True (Popen._check_timeout joins the raw
pipe chunks), so the concat raised TypeError. That TypeError escaped before
_log(), so the timed-out runs left NO entry in their own job log: two missed
ticks, zero diagnostics — only a bare traceback in scheduler.log. The handler
must always be able to write a readable log line, partial output included.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                          # noqa: E402
import app                                             # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


JOB = "test_app_timeout"
LOG = config.PROFILE_HOME / "logs" / f"{JOB}.log"


def main() -> int:
    print("_txt coerces whatever TimeoutExpired carries (bytes included)")
    check("None -> empty", app._txt(None) == "")
    check("bytes -> str", app._txt(b"partial") == "partial")
    check("str stays str", app._txt("partial") == "partial")
    check("undecodable bytes do not raise", isinstance(app._txt(b"\xff\xfe"), str))

    print("run_job reports a timeout instead of dying in its own handler")
    if LOG.exists():
        LOG.unlink()
    app.run_job({
        "name": JOB,
        "cmd": [sys.executable, "-c", "import time, sys; print('partial output'); "
                                      "sys.stdout.flush(); time.sleep(30)"],
        "timeout": 2,
    })
    check("log written (handler did not raise)", LOG.exists())
    body = LOG.read_text() if LOG.exists() else ""
    check("rc=-1 recorded", "rc=-1" in body, body.splitlines()[-1] if body else "empty")
    check("TIMEOUT named", "TIMEOUT after 2s" in body)
    check("partial output survived", "partial output" in body)

    print("a normal (fast) job still reports its own rc and output")
    app.run_job({
        "name": JOB,
        "cmd": [sys.executable, "-c", "print('hello')"],
        "timeout": 30,
    })
    body = LOG.read_text()
    check("rc=0 recorded", "rc=0" in body)
    check("stdout recorded", "hello" in body)
    LOG.unlink(missing_ok=True)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
