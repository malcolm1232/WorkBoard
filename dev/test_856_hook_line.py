#!/usr/bin/env python3
"""#856 - session-start digest line.

Run: python3 dev/test_856_hook_line.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856hook-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")

import _inbox  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def hook_line() -> str:
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "_inbox.py"), "--hook-line"],
        capture_output=True, text=True, env={**os.environ},
    )
    return r.stdout.strip()


def test_silent_when_empty():
    print("empty inbox")
    p = _inbox.path()
    if p.exists():
        p.unlink()
    check(hook_line() == "", "no line when there is nothing to claim")


def test_reports_unclaimed():
    print("unclaimed captures")
    _inbox.append("an idea", update_id=1)
    _inbox.append("another", update_id=2)
    line = hook_line()
    check("2 unclaimed" in line, f"reports the count ({line})")
    check("claim" in line.lower(), "tells the agent how to act on them")


def test_hook_script_wires_it():
    print("hook wiring")
    sh = (REPO / "scripts" / "hook_session_start.sh").read_text()
    check("_inbox.py" in sh and "--hook-line" in sh, "hook calls _inbox.py --hook-line")
    check("${inbox_line}" in sh, "inbox_line interpolated into the session block")
    check("telegram_poller.py" in sh, "hook fires a catch-up poll")
    check("&" in sh.split("telegram_poller.py")[1].split("\n")[0],
          "the catch-up poll is backgrounded so session start stays fast")


if __name__ == "__main__":
    test_silent_when_empty()
    test_reports_unclaimed()
    test_hook_script_wires_it()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
