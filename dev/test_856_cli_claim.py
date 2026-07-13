#!/usr/bin/env python3
"""#856 - card.py claim.

Run: python3 dev/test_856_cli_claim.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856cli-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")

import _inbox  # noqa: E402
import card_commands  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def mk_board():
    bd = Path(tempfile.mkdtemp(prefix="t856board-")) / "board"
    bd.mkdir(parents=True)
    d = {
        "title": "T", "rev": 1, "nextNum": 5, "schemaVersion": 3,
        "columns": [{"id": "notes", "name": "Notes"}, {"id": "task", "name": "Task"}],
        "cards": [], "activeWork": None,
    }
    (bd / "board.json").write_text(json.dumps(d))
    return bd / "board.json", d


def reset_inbox():
    p = _inbox.path()
    if p.exists():
        p.unlink()


def patched_save(saved):
    def fake(p, dd, regen=True):
        saved["d"] = dd
        Path(p).write_text(json.dumps(dd))
        return dd.get("rev", 1) + 1

    return fake


def test_claim_creates_card_and_marks_item():
    print("claim")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("#x look at this https://ex.com/a", update_id=900)

    saved = {}
    real = card_commands.atomic_save
    card_commands.atomic_save = patched_save(saved)
    try:
        args = argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"])
        card_commands.cmd_claim(args, d, board)
    finally:
        card_commands.atomic_save = real

    card = saved["d"]["cards"][0]
    check(card["title"] == "look at this https://ex.com/a", "title from the message, hint stripped")
    check(card["origin"] == "#x look at this https://ex.com/a", "origin is the verbatim message")
    check("from-telegram" in card["tags"], "tagged from-telegram")
    check(card["column"] == "task", "defaults to the task column")
    check(card["meta"]["telegram"]["tid"] == it["tid"], "meta records the inbox id")

    item = _inbox.get(it["tid"])
    check(item["status"] == "claimed", "inbox item claimed")
    check(item["claim"]["cardNum"] == card["num"], "claim records the card number")
    check(_inbox.unclaimed() == [], "item gone from the global column")


def test_claim_respects_explicit_column():
    print("explicit column")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("note this", update_id=901)
    saved = {}
    real = card_commands.atomic_save
    card_commands.atomic_save = patched_save(saved)
    try:
        card_commands.cmd_claim(
            argparse.Namespace(tid=it["tid"], column="notes", ref=it["tid"]), d, board
        )
    finally:
        card_commands.atomic_save = real
    check(saved["d"]["cards"][0]["column"] == "notes", "--column honoured")


def test_double_claim_rejected():
    print("double claim")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("once", update_id=902)
    saved = {}
    real = card_commands.atomic_save
    card_commands.atomic_save = patched_save(saved)
    try:
        card_commands.cmd_claim(argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board)
        try:
            card_commands.cmd_claim(argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board)
            check(False, "second claim must fail")
        except SystemExit:
            check(True, "second claim exits with an error")
    finally:
        card_commands.atomic_save = real


def test_failed_save_releases_reservation():
    print("rollback")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("boom", update_id=903)

    real = card_commands.atomic_save

    def boom(p, dd, regen=True):
        raise RuntimeError("save failed")

    card_commands.atomic_save = boom
    try:
        try:
            card_commands.cmd_claim(argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board)
        except Exception:
            pass
    finally:
        card_commands.atomic_save = real

    check(_inbox.get(it["tid"])["status"] == "unclaimed",
          "a failed save releases the reservation so the item is claimable again")


if __name__ == "__main__":
    test_claim_creates_card_and_marks_item()
    test_claim_respects_explicit_column()
    test_double_claim_rejected()
    test_failed_save_releases_reservation()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
