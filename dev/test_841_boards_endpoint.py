#!/usr/bin/env python3
"""#841: GET /boards lists known projects (path, port, title, running) ordered
by port, plus current_port. Read-only — must not mutate board.json.

Run: python3 dev/test_841_boards_endpoint.py  → exit 0 = green, 1 = a fail.
"""
from __future__ import annotations
import json, sys, importlib
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_fails = 0
def check(cond, msg):
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond: _fails += 1

import serve  # noqa: E402
import port_registry as pr  # noqa: E402


class _Cap:
    """Capture _send(status, body, ...) instead of writing to a socket."""
    def __init__(self): self.status = None; self.body = b""
    def __call__(self, status, body, ctype="application/json", extra=None):
        self.status = status; self.body = body


def make_handler(board_dir, port, cap):
    h = serve.BoardHandler.__new__(serve.BoardHandler)
    h.board_dir = board_dir
    serve.BoardHandler.port = port
    h.path = "/boards"
    h._send = cap
    return h


def test():
    assigns = {"/x/AAA/board": 7891, "/x/BBB/board": 7893, "/x/CCC/board": 7892}
    reg = {"/x/AAA/board": {"port": 7891, "pid": 111},
           "/x/CCC/board": {"port": 7892, "pid": 222}}  # BBB not running

    def fake_title(path):
        return {"/x/AAA/board": "WorkBoard",
                "/x/BBB/board": "QuantifyMe — Work Board",
                "/x/CCC/board": None}.get(str(path))

    cap = _Cap()
    h = make_handler(Path("/x/AAA/board"), 7891, cap)
    with mock.patch.object(pr, "assignments", lambda: assigns), \
         mock.patch.object(pr, "read", lambda: reg), \
         mock.patch.object(pr, "_pid_alive", lambda pid: True), \
         mock.patch.object(serve, "_board_title_for", fake_title):
        h._handle_boards()

    check(cap.status == 200, "responds 200")
    data = json.loads(cap.body)
    ports = [b["port"] for b in data["boards"]]
    check(ports == [7891, 7892, 7893], f"ordered by port asc, got {ports}")
    check(data["current_port"] == 7891, "current_port echoes this board's port")
    by_port = {b["port"]: b for b in data["boards"]}
    check(by_port[7891]["running"] is True, "AAA running (in registry, pid alive)")
    check(by_port[7893]["running"] is False, "BBB not running (absent from registry)")
    check(by_port[7891]["title"] == "WorkBoard", "raw title passed through (norm is client-side)")
    check(by_port[7892]["title"] is None, "missing title → None")


if __name__ == "__main__":
    test()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
