#!/usr/bin/env python3
"""#856 - end to end: fake Telegram -> poller -> inbox -> claim -> real card.

Everything is isolated: a temp inbox, a temp config, throwaway boards. The
live board and the real inbox must be untouched.

Run: python3 dev/test_856_e2e.py
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

_STATE = Path(tempfile.mkdtemp(prefix="t856e2e-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")
Path(os.environ["BOARD_REGISTRY"]).write_text("{}")

LIVE_BOARD = Path.home() / "Desktop" / "WorkBoard" / "board" / "board.json"
LIVE_BEFORE = LIVE_BOARD.read_text() if LIVE_BOARD.exists() else None
LIVE_INBOX = Path.home() / ".board-steward" / "inbox.jsonl"
LIVE_INBOX_BEFORE = LIVE_INBOX.read_text() if LIVE_INBOX.exists() else None

import _inbox  # noqa: E402
import _tg_config as cfg  # noqa: E402
import serve  # noqa: E402
import telegram_poller as tp  # noqa: E402

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


def mk_board(name):
    bd = Path(tempfile.mkdtemp(prefix=f"t856-{name}-")) / "board"
    bd.mkdir(parents=True)
    (bd / "board.json").write_text(json.dumps({
        "title": name, "rev": 1, "nextNum": 1, "schemaVersion": 3,
        "columns": [{"id": "task", "name": "Task"}, {"id": "notes", "name": "Notes"}],
        "cards": [], "activeWork": None,
    }))
    return bd


def handler(bd, cap, body=None):
    h = serve.BoardHandler.__new__(serve.BoardHandler)
    h.board_dir = bd
    serve.BoardHandler.port = 7999
    h._send = cap
    raw = json.dumps(body or {}).encode()
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = BytesIO(raw)
    return h


def fake_api(updates, sent):
    def api(token, method, params, timeout):
        if method == "getUpdates":
            off = int(params.get("offset", 0))
            return {"ok": True, "result": [u for u in updates if u["update_id"] >= off]}
        sent.append(params.get("text"))
        return {"ok": True}

    return api


def main():
    print("end to end")
    cfg.save({"token": "TOK", "chat_id": 7, "offset": 0})
    board_a, board_b = mk_board("a"), mk_board("b")
    sent = []

    # 1. The phone sends two messages.
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 7}, "text": "https://ex.com/security-video"}},
        {"update_id": 2, "message": {"chat": {"id": 7}, "text": "idea: batch the recon sweep"}},
    ]
    serve.broadcast = lambda name, data: None  # no SSE clients in this harness
    new = tp.poll(api=fake_api(updates, sent))
    check(len(new) == 2, "poller captured both messages")
    check(len(sent) == 2 and all("saved" in s for s in sent), "both got a confirmation reply")

    # 2. Both boards show both items in the virtual column.
    for bd, label in ((board_a, "board A"), (board_b, "board B")):
        cap = _Cap()
        handler(bd, cap)._handle_inbox()
        check(len(cap.json()["items"]) == 2, f"{label} shows both captures")

    # 3. Claim the first item on board A, into its notes column.
    tid = new[0]["tid"]
    cap = _Cap()
    handler(board_a, cap, {"tid": tid, "column": "notes"})._handle_inbox_claim()
    check(cap.status == 200, "claim succeeded")
    card = cap.json()["card"]
    check(card["column"] == "notes", "card landed in the column it was dropped on")
    check("from-telegram" in card["tags"], "card is tagged from-telegram")
    check(card["origin"] == "https://ex.com/security-video", "origin is the verbatim message")

    # 4. It is gone from BOTH boards' virtual columns, and board B never got a card.
    for bd, label in ((board_a, "board A"), (board_b, "board B")):
        cap = _Cap()
        handler(bd, cap)._handle_inbox()
        tids = [i["tid"] for i in cap.json()["items"]]
        check(tid not in tids, f"claimed item gone from {label}")
        check(len(tids) == 1, f"{label} still shows the other capture")
    db = json.loads((board_b / "board.json").read_text())
    check(db["cards"] == [], "board B has no card: claiming is exclusive")

    # 5. Board A really has the card on disk.
    da = json.loads((board_a / "board.json").read_text())
    check(len(da["cards"]) == 1 and da["cards"][0]["num"] == card["num"], "card persisted on board A")

    # 6. Claiming it again is refused.
    cap = _Cap()
    handler(board_b, cap, {"tid": tid, "column": "task"})._handle_inbox_claim()
    check(cap.status == 409, "re-claiming from another board is refused")

    # 7. Nothing leaked into the user's real state.
    if LIVE_BEFORE is not None:
        check(LIVE_BOARD.read_text() == LIVE_BEFORE, "the LIVE board is untouched")
    now_inbox = LIVE_INBOX.read_text() if LIVE_INBOX.exists() else None
    check(now_inbox == LIVE_INBOX_BEFORE, "the REAL inbox is untouched")


if __name__ == "__main__":
    main()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
