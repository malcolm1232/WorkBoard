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
check("window.location" in html and "ensure-board" in html, "navigates after ensure")

print("PASS" if _fails == 0 else f"FAIL ({_fails})")
sys.exit(1 if _fails else 0)
