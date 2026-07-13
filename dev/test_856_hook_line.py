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
# Isolated from the user's real ~/.board-steward/telegram.json for the whole
# process (both in-process _tg_config calls AND the --hook-line subprocess,
# which inherits os.environ via hook_line() below).
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")

import _inbox  # noqa: E402
import _tg_config  # noqa: E402

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
    # #856 review MINOR 5 — don't assume "telegram_poller.py" appears exactly
    # once (a comment mentioning the filename earlier in the script would
    # silently make the old sh.split(...)[1] slice inspect the wrong text).
    # Find the line(s) that actually INVOKE it (mention the filename AND
    # python3) and check backgrounding on those specifically.
    invoke_lines = [ln for ln in sh.splitlines()
                    if "telegram_poller.py" in ln and "python3" in ln]
    check(len(invoke_lines) >= 1, "found the line that actually invokes the poller")
    check(bool(invoke_lines) and all("&" in ln for ln in invoke_lines),
          "the catch-up poll is backgrounded so session start stays fast")


def _clear_config():
    p = _tg_config.config_path()
    if p.exists():
        p.unlink()


def _clear_inbox():
    p = _inbox.path()
    if p.exists():
        p.unlink()


def test_tg_status_warns_with_captures_waiting():
    print("telegram broken + captures waiting")
    _clear_inbox()
    _inbox.append("an idea while the bot is broken", update_id=901)
    _tg_config.save({
        "token": "123:ABC", "chat_id": 1, "offset": 0,
        "status": "Telegram token invalid or revoked - re-run `card.py telegram-setup`",
    })
    line = hook_line()
    check("telegram-setup" in line, f"tells the user to reconnect ({line!r})")
    check("CAPTURES" in line, "still reports the waiting captures alongside the warning")


def test_tg_status_warns_with_zero_captures():
    print("telegram broken + inbox empty (the actually-broken state)")
    _clear_inbox()
    _tg_config.save({
        "token": "123:ABC", "chat_id": 1, "offset": 0,
        "status": "Telegram token invalid or revoked - re-run `card.py telegram-setup`",
    })
    line = hook_line()
    check(line != "", "prints even with zero unclaimed - that IS the broken state")
    check("telegram-setup" in line, f"tells the user how to fix it ({line!r})")


def test_tg_status_silent_when_healthy():
    print("telegram healthy config")
    _clear_inbox()
    _tg_config.save({"token": "123:ABC", "chat_id": 1, "offset": 0, "status": None})
    line = hook_line()
    check(line == "", f"no warning when the poller is healthy ({line!r})")


def test_tg_status_silent_when_unconfigured():
    print("no telegram config at all")
    _clear_inbox()
    _clear_config()
    line = hook_line()
    check(line == "", f"no warning and no crash when telegram was never configured ({line!r})")


if __name__ == "__main__":
    test_silent_when_empty()
    test_reports_unclaimed()
    test_hook_script_wires_it()
    test_tg_status_warns_with_captures_waiting()
    test_tg_status_warns_with_zero_captures()
    test_tg_status_silent_when_healthy()
    test_tg_status_silent_when_unconfigured()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
