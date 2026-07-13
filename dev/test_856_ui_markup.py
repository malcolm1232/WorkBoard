#!/usr/bin/env python3
"""#856 - virtual column markup + wiring (static checks on board.html).

Run: python3 dev/test_856_ui_markup.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HTML = (REPO / "templates" / "board.html").read_text()

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def test_wiring():
    print("virtual column wiring")
    check("__inbox__" in HTML, "virtual column id present")
    check("fetchInbox" in HTML, "fetchInbox defined")
    check("renderInboxColumn" in HTML, "renderInboxColumn defined")
    check("commitInboxClaim" in HTML, "commitInboxClaim defined")
    check("discardInboxItem" in HTML, "discardInboxItem defined")
    check("/inbox/claim" in HTML, "claim endpoint called")
    check("/inbox/discard" in HTML, "discard endpoint called")
    check("inbox-updated" in HTML, "SSE inbox-updated listener registered")
    check("addEventListener('focus'" in HTML or 'addEventListener("focus"' in HTML,
          "refetches on window focus")


def test_never_persisted():
    """The single most dangerous bug: a virtual item leaking into state and being saved."""
    print("virtual items never enter state")
    check("state.cards.push(...inboxItems" not in HTML, "inbox items not pushed into state.cards")
    check("state.columns.push({ id: INBOX_COL" not in HTML, "virtual column not pushed into state.columns")
    check(re.search(r"inboxItems\s*=\s*\[\]", HTML) is not None,
          "inboxItems is its own array, separate from state")


def test_claim_branch_precedes_card_move():
    print("drag branch")
    m = re.search(r"function commitCardDrag\s*\([^)]*\)\s*\{(.{0,400})", HTML, re.S)
    check(m is not None, "commitCardDrag found")
    if m:
        head = m.group(1)
        check("inboxTid" in head,
              "commitCardDrag branches on an inbox drag BEFORE looking the card up in state.cards")


def test_taxonomy():
    print("tag taxonomy")
    # #856 review IMPORTANT 3 - templates/board.json must NOT carry a
    # tagTaxonomy block: serve_bootstrap.py unconditionally overwrites
    # data["tagTaxonomy"] from templates/tag-profiles.json on every real
    # board creation, so a block on the raw template never takes effect
    # there, but a HALF taxonomy (one name, no "main") turns
    # card_state._check_tags into a whitelist for any code path that copies
    # the template raw (e.g. skills/e2e/e2e_workboard.py's from_template=True)
    # - silently stripping every ordinary tag. tag-profiles.json is what
    # actually delivers and styles from-telegram, in all profiles.
    board_json = json.loads((REPO / "templates" / "board.json").read_text())
    check("tagTaxonomy" not in board_json,
          "templates/board.json carries no tagTaxonomy block (dead code + live trap)")
    profiles = json.loads((REPO / "templates" / "tag-profiles.json").read_text())
    for name, profile in profiles.items():
        if name.startswith("_"):
            continue
        sub_names = [t["name"] for t in profile.get("sub", [])]
        check("from-telegram" in sub_names,
              f"from-telegram registered in the '{name}' tag-profiles.json profile")


if __name__ == "__main__":
    test_wiring()
    test_never_persisted()
    test_claim_branch_precedes_card_move()
    test_taxonomy()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
