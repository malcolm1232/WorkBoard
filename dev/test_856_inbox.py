#!/usr/bin/env python3
"""#856 - shared capture inbox.

Run: python3 dev/test_856_inbox.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856inbox-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")

import _inbox  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def reset():
    p = _inbox.path()
    if p.exists():
        p.unlink()


def test_append_and_parse():
    print("append + parse")
    reset()
    it = _inbox.append("look at this https://ex.com/a", update_id=101)
    check(it["tid"] == "T-1", "first item is T-1")
    check(it["status"] == "unclaimed", "starts unclaimed")
    check(it["routeHint"] is None, "no hint when no prefix")
    check(it["url"] == "https://ex.com/a", "url extracted")
    check(it["title"] == "look at this https://ex.com/a", "title is the full text")

    it2 = _inbox.append("#qm https://ex.com/b", update_id=102)
    check(it2["tid"] == "T-2", "second item is T-2")
    check(it2["routeHint"] == "qm", "leading #alias parsed as routeHint")
    check(it2["title"] == "https://ex.com/b", "hint stripped from title")
    check(it2["text"] == "#qm https://ex.com/b", "text stays verbatim")


def test_dedupe():
    print("dedupe by update_id")
    reset()
    _inbox.append("a", update_id=200)
    dup = _inbox.append("a again", update_id=200)
    check(dup is None, "same update_id returns None")
    check(len(_inbox.unclaimed()) == 1, "only one item stored")


def test_claim_lifecycle():
    print("reserve / finalize / release / discard")
    reset()
    a = _inbox.append("a", update_id=300)
    _inbox.reserve(a["tid"])
    check(_inbox.get(a["tid"])["status"] == "reserved", "reserve flips to reserved")
    _inbox.finalize(a["tid"], board="/b/board", card_num=431)
    got = _inbox.get(a["tid"])
    check(got["status"] == "claimed", "finalize flips to claimed")
    check(got["claim"]["cardNum"] == 431, "claim records card num")
    check(_inbox.unclaimed() == [], "claimed item leaves the unclaimed list")

    b = _inbox.append("b", update_id=301)
    _inbox.reserve(b["tid"])
    _inbox.release(b["tid"])
    check(_inbox.get(b["tid"])["status"] == "unclaimed", "release restores unclaimed")

    c = _inbox.append("c", update_id=302)
    _inbox.discard(c["tid"])
    check(_inbox.get(c["tid"])["status"] == "discarded", "discard flips to discarded")
    check(len(_inbox.unclaimed()) == 1, "only the released item is unclaimed")


def test_first_wins_under_concurrency():
    print("atomic first-wins reserve")
    reset()
    it = _inbox.append("contested", update_id=400)
    winners, conflicts = [], []

    def go():
        try:
            _inbox.reserve(it["tid"])
            winners.append(1)
        except _inbox.InboxConflict:
            conflicts.append(1)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(len(winners) == 1, f"exactly one reserve wins (got {len(winners)})")
    check(len(conflicts) == 7, f"the other seven conflict (got {len(conflicts)})")


def test_conflict_carries_claim():
    print("conflict reports the winning claim")
    reset()
    it = _inbox.append("x", update_id=500)
    _inbox.reserve(it["tid"])
    _inbox.finalize(it["tid"], board="/qm/board", card_num=77)
    try:
        _inbox.reserve(it["tid"])
        check(False, "second reserve must raise")
    except _inbox.InboxConflict as e:
        check(e.claim["cardNum"] == 77, "InboxConflict carries the winning claim")


def test_corrupt_line_skipped():
    print("corrupt line tolerated")
    reset()
    _inbox.append("good", update_id=600)
    with _inbox.path().open("a") as fh:
        fh.write("{not json\n")
    _inbox.append("also good", update_id=601)
    check(len(_inbox.unclaimed()) == 2, "corrupt line skipped, real items survive")


def test_counts():
    print("counts for the session digest")
    reset()
    check(_inbox.counts()["unclaimed"] == 0, "empty inbox counts zero")
    _inbox.append("a", update_id=700)
    _inbox.append("b", update_id=701)
    c = _inbox.counts()
    check(c["unclaimed"] == 2, "counts unclaimed items")
    check(c["oldest_age_days"] is not None, "reports the oldest age")


if __name__ == "__main__":
    test_append_and_parse()
    test_dedupe()
    test_claim_lifecycle()
    test_first_wins_under_concurrency()
    test_conflict_carries_claim()
    test_corrupt_line_skipped()
    test_counts()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
