#!/usr/bin/env python3
"""#856 - card.py claim.

Run: python3 dev/test_856_cli_claim.py
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
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
# Tests that exercise the REAL card_state.atomic_save (below) must never
# probe real network ports looking for a board server - force the direct-
# write path so they stay hermetic and fast.
os.environ["BOARD_NO_SERVER"] = "1"
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")

import _inbox  # noqa: E402
import card_commands  # noqa: E402
import card_state  # noqa: E402

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


def test_reserved_conflict_wording_distinguishes_from_claimed():
    """#856 review MINOR 3 - a concurrent in-flight reservation must not be
    reported as "already claimed" (untrue: e.claim is None for a reserve-only
    conflict)."""
    print("reserved-but-not-claimed conflict is worded differently from claimed")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("in flight", update_id=910)

    # Simulate a concurrent claimer that reserved but hasn't finalized yet.
    _inbox.reserve(it["tid"])

    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            card_commands.cmd_claim(argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board)
        check(False, "claiming a reserved (not yet claimed) item must fail")
    except SystemExit as e:
        msg = str(e)
        check("already claimed" not in msg, f"does not falsely say 'already claimed': {msg!r}")
        check("reserved" in msg, f"names the real status (reserved): {msg!r}")


def test_regen_failure_does_not_fail_atomic_save():
    """#856 review CRITICAL 1(a) - a regen subprocess timeout/failure must not
    propagate out of atomic_save once the board write itself has landed."""
    print("atomic_save tolerates a hung/failing regen_index subprocess")
    board, d = mk_board()

    real_run = card_state.subprocess.run

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="regen_index.py", timeout=10)

    card_state.subprocess.run = boom
    try:
        rev = card_state.atomic_save(board, dict(d))
    finally:
        card_state.subprocess.run = real_run

    check(rev == 2, "atomic_save returns the new rev instead of raising")
    on_disk = json.loads(board.read_text())
    check(on_disk["rev"] == 2, "the board write landed on disk despite the regen failure")


def test_regen_failure_during_claim_no_duplicate_on_retry():
    """#856 review CRITICAL 1 end-to-end: a regen failure during `claim` must
    not release the reservation, so a human retry can never create a second
    card for the same capture."""
    print("regen failure during claim: reservation held, retry refused, no duplicate card")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("dup risk", update_id=911)

    real_run = card_state.subprocess.run

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="regen_index.py", timeout=10)

    card_state.subprocess.run = boom
    try:
        card_commands.cmd_claim(argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board)
    finally:
        card_state.subprocess.run = real_run

    on_disk = json.loads(board.read_text())
    check(len(on_disk["cards"]) == 1, "exactly one card exists after the regen failure")
    item = _inbox.get(it["tid"])
    check(item["status"] == "claimed", "the item is marked claimed (reservation not stranded/released)")

    # The obvious human retry: claiming the same tid again must be refused,
    # not silently create a second card.
    try:
        card_commands.cmd_claim(argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board)
        check(False, "retrying an already-claimed tid must fail")
    except SystemExit:
        check(True, "retry correctly refused")

    on_disk_after = json.loads(board.read_text())
    check(len(on_disk_after["cards"]) == 1, "still exactly one card after the retry attempt")


def test_finalize_failure_keeps_card_no_crash_prints_line_first():
    """#856 review CRITICAL 1(b): a `finalize` failure AFTER a successful save
    must not crash cmd_claim, must not release the reservation, must still
    print the poller's '+ #<num>' line, and must exit non-zero."""
    print("finalize failure after save: card kept, no crash, poller line first, exit non-zero")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("finalize boom", update_id=912)

    real_finalize = _inbox.finalize

    def boom_finalize(*a, **kw):
        raise TimeoutError("inbox lock busy")

    _inbox.finalize = boom_finalize

    saved = {}
    real_save = card_commands.atomic_save
    card_commands.atomic_save = patched_save(saved)

    buf = io.StringIO()
    exit_code = "not-raised"
    try:
        with contextlib.redirect_stdout(buf):
            try:
                card_commands.cmd_claim(
                    argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board
                )
            except SystemExit as e:
                exit_code = e.code
    finally:
        _inbox.finalize = real_finalize
        card_commands.atomic_save = real_save

    check(exit_code not in (0, None, "not-raised"),
          f"cmd_claim exits non-zero and does not raise an uncaught exception (got {exit_code!r})")

    out_lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    check(bool(out_lines) and out_lines[0].startswith("+ #"),
          f"the poller's '+ #<num>' line is still printed, first: {out_lines[:1]!r}")

    check(bool(saved.get("d")) and len(saved["d"]["cards"]) == 1,
          "the card was actually saved and survives the finalize failure")

    item = _inbox.get(it["tid"])
    check(item["status"] == "reserved",
          "the reservation is NOT released after a successful save, even though finalize failed")


def test_takeover_pre_save_recheck_blocks_duplicate():
    """Closing-the-gap scenario: A reserves T-n, stalls past STALE_RESERVE_S,
    B legitimately re-reserves + finalizes on ITS OWN board, then A proceeds
    to build+save. The pre-save ownership re-check (right before atomic_save)
    must catch the takeover and abort BEFORE writing A's card - not after."""
    print("takeover: pre-save re-check blocks the duplicate before it's written")
    reset_inbox()
    board_a, d_a = mk_board()
    board_b, _d_b = mk_board()
    it = _inbox.append("phone idea", update_id=920)
    tid = it["tid"]

    real_build_card = card_commands.build_card

    def stall_then_takeover(*a, **kw):
        # Models A stalling (laptop sleep / slow disk) right after its own
        # reserve() returned, long enough for the reservation to go stale,
        # during which B legitimately takes over and finalizes on board_b.
        items = _inbox._read()
        for i in items:
            if i["tid"] == tid:
                i["reservedAt"] = i["reservedAt"] - (_inbox.STALE_RESERVE_S + 5)
        _inbox._write(items)
        item_b = _inbox.reserve(tid)
        _inbox.finalize(tid, board=str(board_b.parent), card_num=42, token=item_b["reserveToken"])
        return real_build_card(*a, **kw)

    card_commands.build_card = stall_then_takeover
    saved = {}
    real_save = card_commands.atomic_save
    card_commands.atomic_save = patched_save(saved)
    buf = io.StringIO()
    exit_code = "not-raised"
    try:
        with contextlib.redirect_stderr(buf):
            try:
                card_commands.cmd_claim(
                    argparse.Namespace(tid=tid, column=None, ref=tid), d_a, board_a
                )
            except SystemExit as e:
                exit_code = e.code
    finally:
        card_commands.build_card = real_build_card
        card_commands.atomic_save = real_save

    check(exit_code not in (0, None, "not-raised"), f"A exits non-zero (got {exit_code!r})")
    check(not saved, "A creates NO card - the pre-save ownership re-check caught the takeover")
    # sys.exit(str) doesn't itself print - the message lives in e.code (caught above),
    # not on stderr, since we intercept SystemExit ourselves rather than letting it
    # reach the interpreter's default handler.
    msg = str(exit_code)
    check(str(board_b.parent) in msg, f"message names B's winning board: {msg!r}")
    check("#42" in msg, f"message names B's winning card number: {msg!r}")

    on_disk_a = json.loads(board_a.read_text())
    check(on_disk_a["cards"] == [], "board_a on disk still has zero cards")


def test_same_board_dedupe_guard():
    """A card for this tid already exists on THIS board (e.g. left over from
    an earlier claim whose finalize failed, then the reservation went stale
    and the operator retried). The guard must refuse a second card outright,
    with no race required to trigger it."""
    print("same-board dedupe guard refuses a tid this board already carded")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("dup capture", update_id=930)
    tid = it["tid"]

    existing_card = card_state.build_card(
        d, title="already captured", column="task",
        tags=["from-telegram"], meta={"telegram": {"tid": tid}},
    )
    board.write_text(json.dumps(d))

    saved = {}
    real_save = card_commands.atomic_save
    card_commands.atomic_save = patched_save(saved)
    buf = io.StringIO()
    exit_code = "not-raised"
    try:
        with contextlib.redirect_stderr(buf):
            try:
                card_commands.cmd_claim(
                    argparse.Namespace(tid=tid, column=None, ref=tid), d, board
                )
            except SystemExit as e:
                exit_code = e.code
    finally:
        card_commands.atomic_save = real_save

    check(exit_code not in (0, None, "not-raised"), f"exits non-zero (got {exit_code!r})")
    check(not saved, "no second card is created")
    msg = str(exit_code)  # sys.exit(str) message lives in e.code, not on stderr
    check(f"#{existing_card['num']}" in msg, f"names the existing card: {msg!r}")
    check(len(d["cards"]) == 1, "board still has exactly the one pre-existing card")


def test_rollback_handler_never_crashes_masking_original_exception():
    """The earlier rollback handler was `except Exception: _inbox.release(...);
    raise` - if release() itself raises (because the item was taken over in
    between), that InboxConflict replaces the original exception and the
    real cause is lost. release() must be wrapped so ITS failure can never
    mask or replace the original error."""
    print("rollback: a failing release() must not mask the original save exception")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("boom + stolen", update_id=940)
    tid = it["tid"]

    def boom_and_steal(p, dd, regen=True):
        # Simulate the reservation being taken over by someone else exactly
        # as atomic_save is attempting the write (the residual race window
        # left after the pre-save re-check), and the write itself failing.
        items = _inbox._read()
        for i in items:
            if i["tid"] == tid:
                i["reserveToken"] = "someone-elses-token"
        _inbox._write(items)
        raise RuntimeError("original save failure")

    real_save = card_commands.atomic_save
    card_commands.atomic_save = boom_and_steal
    exit_code = None
    raised = None
    try:
        try:
            card_commands.cmd_claim(
                argparse.Namespace(tid=tid, column=None, ref=tid), d, board
            )
        except SystemExit as e:
            exit_code = e.code
        except Exception as e:
            raised = e
    finally:
        card_commands.atomic_save = real_save

    check(raised is not None and isinstance(raised, RuntimeError),
          f"the ORIGINAL RuntimeError surfaces, not an InboxConflict from release "
          f"(got raised={raised!r}, exit_code={exit_code!r})")
    check(raised is not None and str(raised) == "original save failure",
          f"it really is the original exception: {raised!r}")


def test_finalize_failure_message_branches_on_claim():
    """A finalize failure must tell the truth: if the InboxConflict carries a
    `.claim` (we really were taken over after our own save landed), say a
    duplicate now exists and name the winner. If it doesn't (a transient
    failure, e.g. a lock timeout, and the item is still genuinely ours),
    keep the old 'may reappear' wording."""
    print("finalize-failure message branches correctly on InboxConflict.claim")
    real_finalize = _inbox.finalize
    real_save = card_commands.atomic_save

    # Case 1 — InboxConflict WITH .claim: a real takeover happened.
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("takeover finalize", update_id=950)
    tid = it["tid"]

    def boom_with_claim(*a, **kw):
        raise _inbox.InboxConflict(
            f"{tid} taken over", claim={"board": "/some/other/board", "cardNum": 77})

    _inbox.finalize = boom_with_claim
    card_commands.atomic_save = patched_save({})
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            try:
                card_commands.cmd_claim(
                    argparse.Namespace(tid=tid, column=None, ref=tid), d, board)
                check(False, "must exit non-zero")
            except SystemExit as e:
                check(e.code not in (0, None), "exits non-zero")
    finally:
        _inbox.finalize = real_finalize
        card_commands.atomic_save = real_save

    msg = buf.getvalue()
    check("duplicate" in msg.lower(), f"says the created card is a duplicate: {msg!r}")
    check("#77" in msg, f"names the winning card number: {msg!r}")
    check("may reappear" not in msg, f"does not use the transient wording: {msg!r}")

    # Case 2 — InboxConflict with NO .claim: transient, still genuinely ours.
    reset_inbox()
    board2, d2 = mk_board()
    it2 = _inbox.append("transient finalize", update_id=951)
    tid2 = it2["tid"]

    def boom_no_claim(*a, **kw):
        raise _inbox.InboxConflict(f"{tid2} lock timeout", claim=None)

    _inbox.finalize = boom_no_claim
    card_commands.atomic_save = patched_save({})
    buf2 = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf2):
            try:
                card_commands.cmd_claim(
                    argparse.Namespace(tid=tid2, column=None, ref=tid2), d2, board2)
                check(False, "must exit non-zero")
            except SystemExit as e:
                check(e.code not in (0, None), "exits non-zero")
    finally:
        _inbox.finalize = real_finalize
        card_commands.atomic_save = real_save

    msg2 = buf2.getvalue()
    check("may reappear" in msg2, f"keeps the transient 'may reappear' wording: {msg2!r}")
    # The old wording legitimately mentions "duplicate" as a HYPOTHETICAL risk of a
    # future re-claim ("claiming it again will create a duplicate card") - that's
    # fine. What must NOT appear is a claim that a duplicate ALREADY exists now.
    check("already finalized by" not in msg2 and "should be deleted" not in msg2,
          f"does not falsely assert a duplicate exists right now: {msg2!r}")


if __name__ == "__main__":
    test_claim_creates_card_and_marks_item()
    test_claim_respects_explicit_column()
    test_double_claim_rejected()
    test_failed_save_releases_reservation()
    test_reserved_conflict_wording_distinguishes_from_claimed()
    test_regen_failure_does_not_fail_atomic_save()
    test_regen_failure_during_claim_no_duplicate_on_retry()
    test_finalize_failure_keeps_card_no_crash_prints_line_first()
    test_takeover_pre_save_recheck_blocks_duplicate()
    test_same_board_dedupe_guard()
    test_rollback_handler_never_crashes_masking_original_exception()
    test_finalize_failure_message_branches_on_claim()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
