#!/usr/bin/env python3
"""#856 - shared card constructor.

Run: python3 dev/test_856_build_card.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import card_state  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def board():
    return {
        "rev": 1,
        "nextNum": 10,
        "columns": [{"id": "task", "name": "Task"}, {"id": "done", "name": "Done"}],
        "cards": [],
    }


def test_schema():
    print("card schema")
    d = board()
    c = card_state.build_card(d, title="hello", column="task")
    for k in (
        "num", "id", "code", "priority", "title", "column", "tags", "origin",
        "notes", "writeup", "createdAt", "updatedAt", "doneAt",
        "lastTouchedSubtask", "linkedCards", "subtasks",
    ):
        check(k in c, f"card has {k}")
    check(c["num"] == 10, "num taken from nextNum")
    check(d["nextNum"] == 11, "nextNum bumped")
    check(d["cards"][0] is c, "card appended to the board")
    check(c["doneAt"] is None, "doneAt is None for a non-done column")
    check(c["id"] == "c-hello", "id slugified from the title")


def test_done_column_stamps_doneat():
    print("done column")
    d = board()
    c = card_state.build_card(d, title="shipped", column="done")
    check(c["doneAt"] == c["createdAt"], "doneAt stamped when created in done")


def test_unique_id():
    print("unique ids")
    d = board()
    a = card_state.build_card(d, title="same", column="task")
    b = card_state.build_card(d, title="same", column="task")
    check(a["id"] == "c-same", "first id is the plain slug")
    check(b["id"] == "c-same-2", "second id is suffixed")
    check(card_state.unique_card_id(d, "c-same") == "c-same-3", "unique_card_id skips taken ids")


def test_meta_and_tags_passthrough():
    print("meta + tags")
    d = board()
    c = card_state.build_card(
        d, title="x", column="task", tags=["from-telegram"],
        origin="#qm x", meta={"telegram": {"tid": "T-3"}},
    )
    check(c["tags"] == ["from-telegram"], "tags passed through unvalidated")
    check(c["origin"] == "#qm x", "origin is the verbatim message")
    check(c["meta"]["telegram"]["tid"] == "T-3", "meta passed through")


def test_cmd_add_still_matches():
    """cmd_add must keep producing the same shape after the refactor."""
    print("cmd_add regression")
    import argparse

    import card_commands

    d = board()
    args = argparse.Namespace(
        title="a task", column="task", code=None, id=None, priority="medium",
        tag=[], origin=None, origin_stdin=False, notes=None, notes_stdin=False,
        writeup=None, writeup_stdin=False, created_at=None, force=True, auto=False,
        link=None, pause_ms=None,
    )
    saved = {}
    card_state_ref = card_commands.atomic_save

    def fake_save(p, dd, regen=True):
        saved["d"] = dd
        return 2

    card_commands.atomic_save = fake_save
    try:
        card_commands.cmd_add(args, d, Path("/tmp/nope/board.json"))
    finally:
        card_commands.atomic_save = card_state_ref

    c = saved["d"]["cards"][0]
    check(c["num"] == 10, "cmd_add still assigns from nextNum")
    check(c["title"] == "a task", "cmd_add still sets the title")
    check(c["column"] == "task", "cmd_add still sets the column")
    check(saved["d"]["nextNum"] == 11, "cmd_add still bumps nextNum")
    check("subtasks" in c and "linkedCards" in c, "cmd_add still emits the full schema")


if __name__ == "__main__":
    test_schema()
    test_done_column_stamps_doneat()
    test_unique_id()
    test_meta_and_tags_passthrough()
    test_cmd_add_still_matches()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
