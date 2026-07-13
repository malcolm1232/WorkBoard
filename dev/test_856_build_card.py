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


def _default_add_args(**overrides):
    """A cmd_add Namespace with the same defaults the `add` subparser gives,
    so each branch test only has to override the field(s) it's exercising."""
    import argparse
    base = dict(
        title="a task", column="backlog", code=None, id=None, priority="medium",
        tag=[], origin=None, origin_stdin=False, notes=None, notes_stdin=False,
        writeup=None, writeup_stdin=False, created_at=None, force=False, auto=False,
        auto_source=None, urgent=False, no_auto_urgent=False, link=None, pause_ms=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _run_cmd_add(d, args):
    """Run cmd_add against `d` with atomic_save faked out (never touches a real
    board file), mirroring test_cmd_add_still_matches's pattern. Returns the
    just-added card (last element of d['cards'])."""
    import card_commands

    real_save = card_commands.atomic_save

    def fake_save(p, dd, regen=True):
        return (dd.get("rev") or 1) + 1

    card_commands.atomic_save = fake_save
    try:
        card_commands.cmd_add(args, d, Path("/tmp/nope/board.json"))
    finally:
        card_commands.atomic_save = real_save
    return d["cards"][-1]


def test_cmd_add_still_matches():
    """cmd_add must keep producing the same shape after the refactor."""
    print("cmd_add regression")
    d = board()
    args = _default_add_args(title="a task", column="task", priority="medium", force=True)
    c = _run_cmd_add(d, args)

    check(c["num"] == 10, "cmd_add still assigns from nextNum")
    check(c["title"] == "a task", "cmd_add still sets the title")
    check(c["column"] == "task", "cmd_add still sets the column")
    check(d["nextNum"] == 11, "cmd_add still bumps nextNum")
    check("subtasks" in c and "linkedCards" in c, "cmd_add still emits the full schema")


def test_cmd_add_auto_routes_to_ideas():
    """--auto (with no explicit --column) lands in 'ideas' and stamps meta."""
    print("cmd_add --auto")
    d = board()
    args = _default_add_args(
        title="maybe do this", column="backlog", auto=True, auto_source="telegram:T-9",
    )
    c = _run_cmd_add(d, args)

    check(c["column"] == "ideas", "auto card routes to the ideas column")
    check(any(col["id"] == "ideas" for col in d["columns"]), "ideas column gets created")
    check(c.get("meta", {}).get("autoCreated") is True, "auto card stamps meta.autoCreated")
    check(c.get("meta", {}).get("autoSource") == "telegram:T-9", "auto card stamps meta.autoSource")


def test_cmd_add_auto_respects_explicit_column():
    """--auto with an explicit non-default --column does NOT get redirected to ideas."""
    print("cmd_add --auto + explicit column")
    d = board()
    args = _default_add_args(title="auto but placed", column="task", auto=True)
    c = _run_cmd_add(d, args)

    check(c["column"] == "task", "auto card keeps the caller's explicit column")
    check(c.get("meta", {}).get("autoCreated") is True, "still stamps meta.autoCreated")


def test_cmd_add_urgent_flag_forces_super_urgent():
    """--urgent forces the super-urgent column + critical priority."""
    print("cmd_add --urgent")
    d = board()
    args = _default_add_args(title="handle this", column="backlog", urgent=True, priority="medium")
    c = _run_cmd_add(d, args)

    check(c["column"] == "super-urgent", "--urgent routes to super-urgent")
    check(c["priority"] == "critical", "--urgent bumps priority to critical")
    check(any(col["id"] == "super-urgent" for col in d["columns"]), "super-urgent column gets created")


def test_cmd_add_keyword_urgency_detection():
    """A title matching the urgency keyword scan auto-routes without --urgent."""
    print("cmd_add urgency keyword scan")
    d = board()
    args = _default_add_args(title="Fix payments ASAP", column="backlog", priority="medium")
    c = _run_cmd_add(d, args)

    check(c["column"] == "super-urgent", "urgency keyword ('ASAP') auto-routes to super-urgent")
    check(c["priority"] == "critical", "urgency keyword bumps priority to critical")


def test_cmd_add_no_auto_urgent_opts_out():
    """--no-auto-urgent suppresses the keyword scan (card stays where placed)."""
    print("cmd_add --no-auto-urgent")
    d = board()
    args = _default_add_args(
        title="Fix payments ASAP", column="task", priority="medium", no_auto_urgent=True,
    )
    c = _run_cmd_add(d, args)

    check(c["column"] == "task", "--no-auto-urgent skips the keyword-triggered reroute")
    check(c["priority"] == "medium", "--no-auto-urgent leaves priority untouched")


def test_cmd_add_direct_to_done_stamps_doneat():
    """A card added straight into 'done' gets doneAt stamped through cmd_add."""
    print("cmd_add straight to done")
    d = board()
    args = _default_add_args(title="already shipped", column="done", priority="medium")
    c = _run_cmd_add(d, args)

    check(c["column"] == "done", "card lands in done")
    check(c["doneAt"] is not None, "doneAt is stamped for a card added directly to done")
    check(c["doneAt"] == c["createdAt"], "doneAt matches createdAt for a direct-to-done add")


def test_cmd_add_id_dedupe_same_title():
    """Two adds with the same title dedupe to c-<slug> then c-<slug>-2."""
    print("cmd_add id dedupe")
    d = board()
    a = _run_cmd_add(d, _default_add_args(title="Same Title", column="task", priority="medium"))
    b = _run_cmd_add(d, _default_add_args(title="Same Title", column="task", priority="medium"))

    check(a["id"] == "c-same-title", "first add gets the plain slug id")
    check(b["id"] == "c-same-title-2", "second add with the same title gets the -2 suffix")


def test_cmd_add_link_wiring():
    """--link updates linkedCards on BOTH the new card and the target card."""
    print("cmd_add --link")
    d = board()
    a = _run_cmd_add(d, _default_add_args(title="Card A", column="task", priority="medium"))
    b = _run_cmd_add(d, _default_add_args(
        title="Card B", column="task", priority="medium", link=[str(a["num"])],
    ))

    check(b["id"] in a["linkedCards"], "the linked-to card gets the new card's id")
    check(a["id"] in b["linkedCards"], "the new card gets the linked-to card's id")


def test_cmd_add_tag_taxonomy_enforced_by_cmd_add_not_build_card():
    """_check_tags (cmd_add's policy) rejects an off-taxonomy tag without
    --force; build_card itself must stay policy-free (no validation)."""
    print("cmd_add tag taxonomy vs build_card passthrough")
    d = board()
    d["tagTaxonomy"] = {"main": [{"name": "backend"}], "sub": []}

    rejected = _run_cmd_add(d, _default_add_args(
        title="needs a tag", column="task", priority="medium", tag=["frontend"], force=False,
    ))
    check(rejected["tags"] == [], "cmd_add drops an off-taxonomy tag without --force")

    forced = _run_cmd_add(d, _default_add_args(
        title="needs a tag forced", column="task", priority="medium", tag=["frontend"], force=True,
    ))
    check(forced["tags"] == ["frontend"], "cmd_add keeps an off-taxonomy tag when --force is passed")

    accepted = _run_cmd_add(d, _default_add_args(
        title="on-taxonomy tag", column="task", priority="medium", tag=["backend"], force=False,
    ))
    check(accepted["tags"] == ["backend"], "cmd_add keeps an on-taxonomy tag without needing --force")

    # build_card is called directly here (bypassing cmd_add entirely) with the
    # same taxonomy-bearing board dict — it must NOT filter the tag. Tag policy
    # belongs solely to cmd_add's _check_tags call, per build_card's docstring.
    raw = card_state.build_card(d, title="raw", column="task", tags=["totally-off-taxonomy"])
    check(raw["tags"] == ["totally-off-taxonomy"],
          "build_card itself does not validate tags against the taxonomy")


if __name__ == "__main__":
    test_schema()
    test_done_column_stamps_doneat()
    test_unique_id()
    test_meta_and_tags_passthrough()
    test_cmd_add_still_matches()
    test_cmd_add_auto_routes_to_ideas()
    test_cmd_add_auto_respects_explicit_column()
    test_cmd_add_urgent_flag_forces_super_urgent()
    test_cmd_add_keyword_urgency_detection()
    test_cmd_add_no_auto_urgent_opts_out()
    test_cmd_add_direct_to_done_stamps_doneat()
    test_cmd_add_id_dedupe_same_title()
    test_cmd_add_link_wiring()
    test_cmd_add_tag_taxonomy_enforced_by_cmd_add_not_build_card()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
