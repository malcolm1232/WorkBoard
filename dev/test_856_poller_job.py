#!/usr/bin/env python3
"""#856 - the 15-minute poller job.

Run: python3 dev/test_856_poller_job.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import install_launchd as il  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def test_plist_shape():
    print("poller plist")
    p = il.build_poller_plist(REPO / "scripts" / "telegram_poller.py")
    check(p["Label"] == "com.boardsteward.telegram", "one global label, not per board")
    check(p.get("StartInterval") == 900, "fires every 15 minutes")
    check("KeepAlive" not in p,
          "KeepAlive must NOT be set: launchd would respawn the short-lived poller in a tight loop")
    check(str(p["ProgramArguments"][-1]).endswith("telegram_poller.py"), "runs the poller")
    check("Logs" in p["StandardErrorPath"] or "log" in p["StandardErrorPath"].lower(),
          "errors are logged somewhere findable")


def test_install_is_idempotent_dry_run():
    print("dry run")
    a = il.install_poller(dry_run=True)
    b = il.install_poller(dry_run=True)
    check(a == b, "same plist path each time")
    check(str(a).endswith("com.boardsteward.telegram.plist"), "plist path derived from the label")


if __name__ == "__main__":
    test_plist_shape()
    test_install_is_idempotent_dry_run()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
