#!/usr/bin/env python3
"""#856 - shared capture inbox.

Run: python3 dev/test_856_inbox.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
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
    reserved_a = _inbox.reserve(a["tid"])
    check(_inbox.get(a["tid"])["status"] == "reserved", "reserve flips to reserved")
    check(bool(reserved_a.get("reserveToken")), "reserve returns a reserveToken")
    _inbox.finalize(a["tid"], board="/b/board", card_num=431, token=reserved_a["reserveToken"])
    got = _inbox.get(a["tid"])
    check(got["status"] == "claimed", "finalize flips to claimed")
    check(got["claim"]["cardNum"] == 431, "claim records card num")
    check(got.get("reserveToken") is None, "finalize clears the reserveToken")
    check(_inbox.unclaimed() == [], "claimed item leaves the unclaimed list")

    b = _inbox.append("b", update_id=301)
    reserved_b = _inbox.reserve(b["tid"])
    _inbox.release(b["tid"], token=reserved_b["reserveToken"])
    got_b = _inbox.get(b["tid"])
    check(got_b["status"] == "unclaimed", "release restores unclaimed")
    check(got_b.get("reserveToken") is None, "release clears the reserveToken")

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
    reserved = _inbox.reserve(it["tid"])
    _inbox.finalize(it["tid"], board="/qm/board", card_num=77, token=reserved["reserveToken"])
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


def test_stale_reserve_self_heals():
    print("stale reserve self-heals")
    reset()
    it = _inbox.append("stale", update_id=800)
    first = _inbox.reserve(it["tid"])

    # A fresh reserve is still held: a second reserve must block.
    try:
        _inbox.reserve(it["tid"])
        check(False, "fresh reserve must still block a second reserve")
    except _inbox.InboxConflict:
        check(True, "fresh reserve still blocks a second reserve")

    # Age the reservation past STALE_RESERVE_S without sleeping.
    got = _inbox.get(it["tid"])
    got["reservedAt"] = time.time() - (_inbox.STALE_RESERVE_S + 1)
    items = [
        (got if i["tid"] == it["tid"] else i)
        for i in _inbox._read()  # noqa: SLF001 - whitebox test of the module it covers
    ]
    _inbox._write(items)  # noqa: SLF001

    free_items = _inbox.unclaimed()
    check(it["tid"] in [i["tid"] for i in free_items], "stale reserve reappears as unclaimed")
    stale_payload = next(i for i in free_items if i["tid"] == it["tid"])
    check("reserveToken" not in stale_payload, "unclaimed() strips the reserveToken before it reaches a browser")

    # And because it is stale, a new reserve now succeeds instead of conflicting.
    reclaimed = _inbox.reserve(it["tid"])
    check(reclaimed["status"] == "reserved", "stale reserve is reclaimable")
    check(reclaimed["reserveToken"] != first["reserveToken"], "takeover mints a fresh reserveToken")


def test_wrong_state_transitions_conflict():
    print("wrong-state transitions raise InboxConflict")
    reset()

    # finalize without reserve first.
    a = _inbox.append("a", update_id=900)
    try:
        _inbox.finalize(a["tid"], board="/board-a", card_num=1, token="never-reserved")
        check(False, "finalize on unclaimed item must raise")
    except _inbox.InboxConflict:
        check(True, "finalize-without-reserve raises InboxConflict")

    # release on a claimed item.
    b = _inbox.append("b", update_id=901)
    reserved_b = _inbox.reserve(b["tid"])
    _inbox.finalize(b["tid"], board="/board-b", card_num=2, token=reserved_b["reserveToken"])
    try:
        _inbox.release(b["tid"], token=reserved_b["reserveToken"])
        check(False, "release on claimed item must raise")
    except _inbox.InboxConflict:
        check(True, "release-on-claimed raises InboxConflict")
    check(_inbox.get(b["tid"])["status"] == "claimed", "release-on-claimed left status untouched")

    # discard on a claimed item.
    c = _inbox.append("c", update_id=902)
    reserved_c = _inbox.reserve(c["tid"])
    _inbox.finalize(c["tid"], board="/board-c", card_num=3, token=reserved_c["reserveToken"])
    try:
        _inbox.discard(c["tid"])
        check(False, "discard on claimed item must raise")
    except _inbox.InboxConflict:
        check(True, "discard-on-claimed raises InboxConflict")
    check(_inbox.get(c["tid"])["status"] == "claimed", "discard-on-claimed left status untouched")

    # double finalize.
    d = _inbox.append("d", update_id=903)
    reserved_d = _inbox.reserve(d["tid"])
    _inbox.finalize(d["tid"], board="/board-d", card_num=4, token=reserved_d["reserveToken"])
    try:
        _inbox.finalize(d["tid"], board="/board-d2", card_num=5, token=reserved_d["reserveToken"])
        check(False, "double finalize must raise")
    except _inbox.InboxConflict:
        check(True, "double-finalize raises InboxConflict")
    check(_inbox.get(d["tid"])["claim"]["cardNum"] == 4, "double-finalize did not overwrite the original claim")

    # finalize with a wrong/stale token on a still-reserved item.
    e = _inbox.append("e", update_id=904)
    reserved_e = _inbox.reserve(e["tid"])
    try:
        _inbox.finalize(e["tid"], board="/board-e", card_num=6, token="not-the-real-token")
        check(False, "finalize with a wrong token must raise")
    except _inbox.InboxConflict:
        check(True, "finalize-with-wrong-token raises InboxConflict")
    check(_inbox.get(e["tid"])["status"] == "reserved", "wrong-token finalize left status untouched")
    _inbox.finalize(e["tid"], board="/board-e", card_num=6, token=reserved_e["reserveToken"])
    check(_inbox.get(e["tid"])["status"] == "claimed", "the correct token still finalizes fine")


def test_duplicate_claim_regression():
    print("reviewer repro: release-then-reclaim can no longer duplicate a claim")
    reset()
    a = _inbox.append("y", update_id=2)
    reserved_a = _inbox.reserve(a["tid"])
    token_a = reserved_a["reserveToken"]
    _inbox.finalize(a["tid"], "/board-a", 1, token=token_a)  # claimed by A

    try:
        _inbox.release(a["tid"], token=token_a)  # must NOT silently un-claim it
        check(False, "release on a claimed item must raise, not un-claim it")
    except _inbox.InboxConflict:
        check(True, "release on claimed item is refused")

    check(_inbox.get(a["tid"])["status"] == "claimed", "item is still claimed after the refused release")

    try:
        _inbox.reserve(a["tid"])
        check(False, "reserve on a claimed item must raise")
    except _inbox.InboxConflict as e:
        check(True, "reserve on claimed item is refused")
        check(e.claim["cardNum"] == 1, "conflict still reports board A's original claim")

    try:
        _inbox.finalize(a["tid"], "/board-b", 2, token=token_a)
        check(False, "board B must not be able to also claim this item")
    except _inbox.InboxConflict:
        check(True, "second claim by board B is refused")

    check(_inbox.get(a["tid"])["claim"]["board"] == "/board-a", "item is still claimed by board A only")
    check(_inbox.get(a["tid"])["claim"]["cardNum"] == 1, "item still carries board A's card number")


def test_stale_takeover_cannot_produce_two_claims():
    print("reviewer repro #2: stalled process A cannot win after B legally takes over a stale reserve")
    reset()
    it = _inbox.append("takeover", update_id=1000)

    # 1. Process A reserves T-1.
    reserved_a = _inbox.reserve(it["tid"])
    token_a = reserved_a["reserveToken"]
    check(bool(token_a), "A's reserve returns a reserveToken")

    # 2. More than STALE_RESERVE_S passes (aged without sleeping) and B
    #    legally re-reserves the now-stale item, minting a fresh token.
    got = _inbox.get(it["tid"])
    got["reservedAt"] = time.time() - (_inbox.STALE_RESERVE_S + 1)
    items = [
        (got if i["tid"] == it["tid"] else i)
        for i in _inbox._read()  # noqa: SLF001 - whitebox test of the module it covers
    ]
    _inbox._write(items)  # noqa: SLF001

    reserved_b = _inbox.reserve(it["tid"])
    token_b = reserved_b["reserveToken"]
    check(bool(token_b), "B's reserve returns a reserveToken")
    check(token_b != token_a, "B's takeover mints a token different from A's stale one")

    # 3. Process A wakes up and calls finalize with its now-invalid token:
    #    this must be refused, not silently win.
    try:
        _inbox.finalize(it["tid"], board="/board-a", card_num=111, token=token_a)
        check(False, "A's finalize with its stale token must raise InboxConflict")
    except _inbox.InboxConflict:
        check(True, "A's finalize with its stale token is refused")
    check(_inbox.get(it["tid"])["status"] == "reserved", "A's refused finalize left the item reserved (by B)")

    # 4. B's finalize with its correct token succeeds.
    claimed = _inbox.finalize(it["tid"], board="/board-b", card_num=222, token=token_b)
    check(claimed["status"] == "claimed", "B's finalize succeeds")

    final = _inbox.get(it["tid"])
    check(final["claim"]["board"] == "/board-b", "the one true claim belongs to B's board")
    check(final["claim"]["cardNum"] == 222, "the one true claim carries B's card number")


def test_confirmation_tracking():
    print("confirmed field + mark_confirmed + unconfirmed()")
    reset()
    a = _inbox.append("a", update_id=1100)
    b = _inbox.append("b", update_id=1101)
    check(a["confirmed"] is False, "append starts an item unconfirmed")
    check({i["tid"] for i in _inbox.unconfirmed()} == {a["tid"], b["tid"]},
          "both fresh items are unconfirmed")

    confirmed_a = _inbox.mark_confirmed(a["tid"])
    check(confirmed_a["confirmed"] is True, "mark_confirmed returns the updated item")
    check(_inbox.get(a["tid"])["confirmed"] is True, "mark_confirmed persists to disk")
    check([i["tid"] for i in _inbox.unconfirmed()] == [b["tid"]],
          "a confirmed item drops out of unconfirmed(), the other remains")

    try:
        _inbox.mark_confirmed("T-does-not-exist")
        check(False, "mark_confirmed on an unknown tid must raise")
    except KeyError:
        check(True, "mark_confirmed on an unknown tid raises KeyError")

    # Confirmation is orthogonal to the claim lifecycle: a claimed item can
    # still be (un)confirmed independently.
    reserved_b = _inbox.reserve(b["tid"])
    _inbox.finalize(b["tid"], board="/board-b", card_num=9, token=reserved_b["reserveToken"])
    check(_inbox.get(b["tid"])["status"] == "claimed", "b is claimed")
    check([i["tid"] for i in _inbox.unconfirmed()] == [b["tid"]],
          "a claimed-but-unconfirmed item still surfaces in unconfirmed()")
    _inbox.mark_confirmed(b["tid"])
    check(_inbox.unconfirmed() == [], "both items confirmed: unconfirmed() is empty")
    check(_inbox.get(b["tid"])["status"] == "claimed", "confirming does not disturb claim status")

    c = _inbox.append("c", update_id=1102)
    reserved_c = _inbox.reserve(c["tid"])
    check(bool(reserved_c.get("reserveToken")), "c is reserved and holds a token in storage")
    unconfirmed_c = next(i for i in _inbox.unconfirmed() if i["tid"] == c["tid"])
    check("reserveToken" not in unconfirmed_c,
          "unconfirmed() strips reserveToken before it reaches a browser, like unclaimed() does")


def test_legacy_item_without_confirmed_key_treated_as_confirmed():
    print("legacy item with no confirmed key is treated as already confirmed")
    reset()
    _inbox.path().parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "tid": "T-1",
        "update_id": 1200,
        "text": "legacy capture",
        "title": "legacy capture",
        "url": "",
        "routeHint": None,
        "ts": "2020-01-01T00:00:00Z",
        "status": "unclaimed",
        "claim": None,
        "reserveToken": None,
        # NOTE: no "confirmed" key at all - simulates an item written to
        # disk before the field existed.
    }
    with _inbox.path().open("a") as fh:
        fh.write(json.dumps(legacy) + "\n")

    check(legacy["tid"] not in [i["tid"] for i in _inbox.unconfirmed()],
          "a legacy item with no confirmed key does not appear in unconfirmed()")
    check(legacy["tid"] in [i["tid"] for i in _inbox.unclaimed()],
          "the legacy item is otherwise a completely normal unclaimed item")

    # A brand-new capture must be unaffected: append() always stamps
    # confirmed=False explicitly, so it still gets exactly one reply cycle.
    fresh = _inbox.append("fresh capture", update_id=1201)
    check(fresh["confirmed"] is False, "a freshly appended item still starts unconfirmed")
    check([i["tid"] for i in _inbox.unconfirmed()] == [fresh["tid"]],
          "only the fresh item shows up in unconfirmed(), not the legacy one")


def test_notify_boards_never_raises():
    print("notify_boards never raises when nothing is listening")
    reset()
    registry = _STATE / "dead_registry.json"
    registry.write_text(json.dumps({"dead-board": {"port": 59999}}))
    prev = os.environ.get("BOARD_REGISTRY")
    os.environ["BOARD_REGISTRY"] = str(registry)
    try:
        try:
            _inbox.notify_boards()
            check(True, "notify_boards returns cleanly with a dead port in the registry")
        except Exception as e:  # noqa: BLE001 - this is exactly what must never happen
            check(False, f"notify_boards must never raise, got {e!r}")
    finally:
        if prev is None:
            os.environ.pop("BOARD_REGISTRY", None)
        else:
            os.environ["BOARD_REGISTRY"] = prev


if __name__ == "__main__":
    test_append_and_parse()
    test_dedupe()
    test_claim_lifecycle()
    test_first_wins_under_concurrency()
    test_conflict_carries_claim()
    test_corrupt_line_skipped()
    test_counts()
    test_stale_reserve_self_heals()
    test_wrong_state_transitions_conflict()
    test_duplicate_claim_regression()
    test_stale_takeover_cannot_produce_two_claims()
    test_confirmation_tracking()
    test_legacy_item_without_confirmed_key_treated_as_confirmed()
    test_notify_boards_never_raises()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
