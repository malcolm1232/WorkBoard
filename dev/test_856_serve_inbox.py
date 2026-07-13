#!/usr/bin/env python3
"""#856 - server inbox endpoints.

Run: python3 dev/test_856_serve_inbox.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856serve-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")

import _inbox  # noqa: E402
import serve  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


class _Cap:
    def __init__(self):
        self.status = None
        self.body = b""

    def __call__(self, status, body, ctype="application/json", extra=None):
        self.status = status
        self.body = body

    def json(self):
        return json.loads(self.body.decode())


def mk_board():
    bd = Path(tempfile.mkdtemp(prefix="t856sb-")) / "board"
    bd.mkdir(parents=True)
    (bd / "board.json").write_text(json.dumps({
        "title": "T", "rev": 3, "nextNum": 20, "schemaVersion": 3,
        "columns": [{"id": "notes", "name": "Notes"}, {"id": "task", "name": "Task"}],
        "cards": [], "activeWork": None,
    }))
    return bd


def handler(board_dir, cap, body: dict | None = None, path="/inbox"):
    h = serve.BoardHandler.__new__(serve.BoardHandler)
    h.board_dir = board_dir
    serve.BoardHandler.port = 7999
    h.path = path
    h._send = cap
    raw = json.dumps(body or {}).encode()
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = BytesIO(raw)
    return h


def reset_inbox():
    p = _inbox.path()
    if p.exists():
        p.unlink()


def test_get_inbox_is_pure_read():
    print("GET /inbox")
    reset_inbox()
    bd = mk_board()
    _inbox.append("idea one", update_id=1)
    _inbox.append("idea two", update_id=2)
    before = (bd / "board.json").read_text()

    cap = _Cap()
    handler(bd, cap)._handle_inbox()
    check(cap.status == 200, "200")
    check(len(cap.json()["items"]) == 2, "returns both unclaimed items")
    check((bd / "board.json").read_text() == before, "GET has no side effects on board.json")


def test_claim_creates_card():
    print("POST /inbox/claim")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("claim me https://ex.com/x", update_id=3)
    events = []
    real = serve.broadcast
    serve.broadcast = lambda name, data: events.append(name)
    try:
        cap = _Cap()
        handler(bd, cap, {"tid": it["tid"], "column": "notes"})._handle_inbox_claim()
    finally:
        serve.broadcast = real

    check(cap.status == 200, f"200 (got {cap.status})")
    card = cap.json()["card"]
    check(card["column"] == "notes", "card lands in the dropped-on column")
    check("from-telegram" in card["tags"], "tagged from-telegram")
    check(card["origin"] == "claim me https://ex.com/x", "origin is the verbatim message")

    d = json.loads((bd / "board.json").read_text())
    check(len(d["cards"]) == 1, "card persisted to board.json")
    check(d["rev"] == 4, "rev bumped")
    check(d["nextNum"] == 21, "nextNum bumped")
    check("card-added" in events, "card-added broadcast")
    check("inbox-updated" in events, "inbox-updated broadcast")

    check(_inbox.get(it["tid"])["status"] == "claimed", "inbox item claimed")
    check(_inbox.unclaimed() == [], "item leaves the global column")


def test_double_claim_409():
    """The scenario that matters: TWO DIFFERENT boards racing for the same
    capture, not one board claiming twice. Exactly one board wins; the loser
    is refused (before it ever touches its own board.json) and told who won."""
    print("claim race (two different boards)")
    reset_inbox()
    bd1 = mk_board()
    bd2 = mk_board()
    it = _inbox.append("contested", update_id=4)
    before2 = (bd2 / "board.json").read_bytes()

    real = serve.broadcast
    serve.broadcast = lambda name, data: None
    try:
        cap1 = _Cap()
        handler(bd1, cap1, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
        cap2 = _Cap()
        handler(bd2, cap2, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
    finally:
        serve.broadcast = real

    check(cap1.status == 200, f"board 1 wins with 200 (got {cap1.status})")
    check(cap2.status == 409, f"board 2 loses with 409 (got {cap2.status})")
    check(cap2.json()["claim"]["cardNum"] == cap1.json()["card"]["num"],
          "409 names the winning card so the UI can toast it")
    check(cap2.json()["claim"]["board"] == str(bd1),
          "409 also names the winning BOARD, not just the card number")

    d1 = json.loads((bd1 / "board.json").read_text())
    check(len(d1["cards"]) == 1, "the winning board has exactly one card")

    after2 = (bd2 / "board.json").read_bytes()
    check(after2 == before2, "the losing board's board.json is byte-for-byte untouched")


def test_claim_unknown_tid_404():
    print("unknown tid")
    reset_inbox()
    bd = mk_board()
    cap = _Cap()
    handler(bd, cap, {"tid": "T-999", "column": "task"})._handle_inbox_claim()
    check(cap.status == 404, f"404 (got {cap.status})")


def test_discard():
    print("POST /inbox/discard")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("junk", update_id=5)
    real = serve.broadcast
    events = []
    serve.broadcast = lambda name, data: events.append(name)
    try:
        cap = _Cap()
        handler(bd, cap, {"tid": it["tid"]})._handle_inbox_discard()
    finally:
        serve.broadcast = real
    check(cap.status == 200, "200")
    check(_inbox.get(it["tid"])["status"] == "discarded", "item discarded")
    check("inbox-updated" in events, "inbox-updated broadcast")
    d = json.loads((bd / "board.json").read_text())
    check(d["cards"] == [], "discard never creates a card")


def test_notify_broadcasts():
    print("POST /inbox/notify")
    reset_inbox()
    bd = mk_board()
    _inbox.append("fresh", update_id=6)
    events = []
    real = serve.broadcast
    serve.broadcast = lambda name, data: events.append((name, data))
    try:
        cap = _Cap()
        handler(bd, cap, {})._handle_inbox_notify()
    finally:
        serve.broadcast = real
    check(cap.status == 200, "200")
    names = [n for n, _ in events]
    check("inbox-updated" in names, "broadcasts inbox-updated")
    payload = dict(events)["inbox-updated"]
    check(len(payload["items"]) == 1, "payload carries the unclaimed items")


def test_claim_same_board_duplicate_guard():
    print("same-board duplicate guard")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("dup me", update_id=7)
    real = serve.broadcast
    serve.broadcast = lambda name, data: None
    try:
        cap1 = _Cap()
        handler(bd, cap1, {"tid": it["tid"], "column": "notes"})._handle_inbox_claim()
        check(cap1.status == 200, "first claim creates the card")

        # Simulate a stale reservation that got taken over and re-reserved
        # against the SAME board (e.g. a retry after a failed finalize).
        # Force the item back to unclaimed so reserve() succeeds again, then
        # verify the duplicate-card guard refuses to create a second card.
        items = _inbox._read()
        for i in items:
            if i["tid"] == it["tid"]:
                i["status"] = "unclaimed"
                i["claim"] = None
                i["reserveToken"] = None
        _inbox._write(items)

        cap2 = _Cap()
        handler(bd, cap2, {"tid": it["tid"], "column": "notes"})._handle_inbox_claim()
    finally:
        serve.broadcast = real

    check(cap2.status == 409, f"second claim on same board refused (got {cap2.status})")
    d = json.loads((bd / "board.json").read_text())
    check(len(d["cards"]) == 1, "still only one card - duplicate guard held")


def test_claim_atomic_write_failure_releases_and_retries():
    """The write-failure path was untested. Force atomic_write to raise (e.g.
    a malformed board or a disk error) and confirm the handler mirrors
    cmd_claim: clean 500, the reservation is actually released (not stranded
    until the 120s stale self-heal), and a RETRY (no monkeypatch) succeeds in
    creating exactly one card."""
    print("atomic_write failure releases the reservation; retry succeeds")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("stranded me not", update_id=100)
    real_write = serve.atomic_write
    real_broadcast = serve.broadcast
    serve.broadcast = lambda name, data: None

    def _boom(path, data):
        raise OSError("disk full (simulated)")

    serve.atomic_write = _boom
    try:
        cap = _Cap()
        handler(bd, cap, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
    finally:
        serve.atomic_write = real_write
        serve.broadcast = real_broadcast

    check(cap.status == 500, f"write failure -> clean 500, not a dropped connection (got {cap.status})")
    check("disk full" in cap.json().get("error", ""), "500 names the original failure")

    d = json.loads((bd / "board.json").read_text())
    check(d["cards"] == [], "no card written when atomic_write fails")

    item = _inbox.get(it["tid"])
    check(item["status"] == "unclaimed", "reservation released back to unclaimed, not stranded")
    check(item["reserveToken"] is None, "reserveToken cleared on release")

    real_broadcast2 = serve.broadcast
    serve.broadcast = lambda name, data: None
    try:
        cap2 = _Cap()
        handler(bd, cap2, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
    finally:
        serve.broadcast = real_broadcast2

    check(cap2.status == 200, f"retry after the transient failure succeeds (got {cap2.status})")
    d2 = json.loads((bd / "board.json").read_text())
    check(len(d2["cards"]) == 1, "retry created exactly one card (no strand, no duplicate)")


def test_claim_write_failure_when_release_also_fails():
    """A failing release() must never mask the ORIGINAL failure. Force BOTH
    atomic_write and _inbox.release to raise; the 500 must still name the
    write failure (not the release failure), and the handler must not
    crash (no exception escapes _handle_inbox_claim)."""
    print("write failure + release failure: original error still surfaces, no crash")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("double trouble", update_id=101)
    real_write = serve.atomic_write
    real_release = _inbox.release
    real_broadcast = serve.broadcast
    serve.broadcast = lambda name, data: None

    def _boom_write(path, data):
        raise OSError("write exploded (simulated)")

    def _boom_release(tid, token):
        raise RuntimeError("release exploded too (simulated)")

    serve.atomic_write = _boom_write
    _inbox.release = _boom_release
    try:
        cap = _Cap()
        handler(bd, cap, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
    finally:
        serve.atomic_write = real_write
        _inbox.release = real_release
        serve.broadcast = real_broadcast

    check(cap.status == 500, f"still a clean 500, no crash (got {cap.status})")
    check("write exploded" in cap.json().get("error", ""),
          "500 names the ORIGINAL write failure, not the release failure")
    d = json.loads((bd / "board.json").read_text())
    check(d["cards"] == [], "no card written")


def test_claim_build_card_failure_releases_and_retries():
    """Finding-1 coverage: everything from the column lookup through
    build_card must share the SAME release-guarded try/except as
    atomic_write. Force build_card to raise (e.g. a malformed board missing
    nextNum) and confirm the same guarantees as the write-failure path: a
    clean 500 (not a dropped connection), the reservation released, and a
    retry succeeds."""
    print("build_card failure releases the reservation; retry succeeds")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("card build boom", update_id=102)
    import card_state
    real_build_card = card_state.build_card
    real_broadcast = serve.broadcast
    serve.broadcast = lambda name, data: None

    def _boom(*a, **kw):
        raise KeyError("nextNum")

    card_state.build_card = _boom
    try:
        cap = _Cap()
        handler(bd, cap, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
    finally:
        card_state.build_card = real_build_card
        serve.broadcast = real_broadcast

    check(cap.status == 500, f"build_card failure -> clean 500, not a dropped connection (got {cap.status})")
    d = json.loads((bd / "board.json").read_text())
    check(d["cards"] == [], "no card written when build_card fails")

    item = _inbox.get(it["tid"])
    check(item["status"] == "unclaimed", "reservation released, not stranded")

    real_broadcast2 = serve.broadcast
    serve.broadcast = lambda name, data: None
    try:
        cap2 = _Cap()
        handler(bd, cap2, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
    finally:
        serve.broadcast = real_broadcast2

    check(cap2.status == 200, f"retry succeeds after the transient build_card failure (got {cap2.status})")
    d2 = json.loads((bd / "board.json").read_text())
    check(len(d2["cards"]) == 1, "retry created exactly one card")


def test_claim_prewrite_takeover_aborts_before_write():
    """Minor-4 coverage: simulate a stale-reservation takeover happening
    between reserve() and the pre-write ownership re-check (e.g. our
    reservation stalled past STALE_RESERVE_S and another board took over
    and finalized first). The handler must abort BEFORE writing any card,
    respond 409 naming the winner, and NOT call release() - the reservation
    is no longer ours to release."""
    print("pre-write ownership re-check catches a mid-call takeover")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("stolen mid-flight", update_id=103)
    real_get = _inbox.get
    real_broadcast = serve.broadcast
    serve.broadcast = lambda name, data: None

    calls = {"n": 0}
    winner_claim = {"board": "/some/other/board", "cardNum": 77}

    def _fake_get(tid):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_get(tid)  # the handler's initial "does tid exist" check
        # the pre-write re-check: report a takeover by a different claimer
        return {"tid": tid, "status": "claimed", "reserveToken": None,
                "claim": winner_claim}

    _inbox.get = _fake_get
    try:
        cap = _Cap()
        handler(bd, cap, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
    finally:
        _inbox.get = real_get
        serve.broadcast = real_broadcast

    check(cap.status == 409, f"mid-call takeover -> 409 (got {cap.status})")
    check(cap.json()["claim"] == winner_claim, "409 names the winning board+card")

    d = json.loads((bd / "board.json").read_text())
    check(d["cards"] == [], "aborted BEFORE writing any card")

    real_item = real_get(it["tid"])
    check(real_item["status"] == "reserved",
          "did not release the reservation it no longer owns (still genuinely reserved)")


def test_configured_flag():
    print("GET /inbox configured flag")
    reset_inbox()
    bd = mk_board()
    cap = _Cap()
    handler(bd, cap)._handle_inbox()
    check(cap.json()["configured"] is False, "no telegram config -> configured false")


def test_reserve_token_never_leaks_to_browser():
    print("reserveToken never leaks")
    reset_inbox()
    bd = mk_board()
    _inbox.append("secret token check", update_id=8)
    cap = _Cap()
    handler(bd, cap)._handle_inbox()
    items = cap.json()["items"]
    check(all("reserveToken" not in i for i in items), "no reserveToken in GET /inbox payload")


if __name__ == "__main__":
    test_get_inbox_is_pure_read()
    test_claim_creates_card()
    test_double_claim_409()
    test_claim_unknown_tid_404()
    test_discard()
    test_notify_broadcasts()
    test_claim_same_board_duplicate_guard()
    test_claim_atomic_write_failure_releases_and_retries()
    test_claim_write_failure_when_release_also_fails()
    test_claim_build_card_failure_releases_and_retries()
    test_claim_prewrite_takeover_aborts_before_write()
    test_configured_flag()
    test_reserve_token_never_leaks_to_browser()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
