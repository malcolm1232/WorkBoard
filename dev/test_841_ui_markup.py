#!/usr/bin/env python3
"""#841: the served board.html must carry the project-switcher hooks — the
#project-tabs container, a loadProjectTabs() that fetches /boards, and a click
path that POSTs /ensure-board then navigates. Static markup/JS presence check.

Run: python3 dev/test_841_ui_markup.py  → exit 0 = green, 1 = a fail.
"""
from __future__ import annotations
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
html = (REPO / "templates" / "board.html").read_text()

_fails = 0
def check(cond, msg):
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond: _fails += 1

check('id="project-tabs"' in html, "has #project-tabs container")
check("loadProjectTabs" in html, "defines loadProjectTabs()")
check("/boards" in html, "fetches /boards")
check("/ensure-board" in html, "calls /ensure-board")
check("cleanProjectTitle(" in html, "labels via cleanProjectTitle")
# #846 — the switcher swaps the on-screen board IN PLACE (activateBoard) after
# /ensure-board; it must NOT navigate (a full reload drops the home server + SSE).
import re
_sw = re.search(r"/ensure-board\?path=.*?\n\}\n", html, re.S)
check(bool(_sw) and "activateBoard(" in _sw.group(0), "swaps in place via activateBoard() after ensure")
check(bool(_sw) and "location.href" not in _sw.group(0) and "location.assign" not in _sw.group(0),
      "does not navigate away after ensure")

print("PASS" if _fails == 0 else f"FAIL ({_fails})")
sys.exit(1 if _fails else 0)
