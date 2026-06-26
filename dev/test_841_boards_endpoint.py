#!/usr/bin/env python3
"""#841: GET /boards lists known projects (path, port, title, running) ordered
by port, plus current_port. Read-only — must not mutate board.json.

Run: python3 dev/test_841_boards_endpoint.py  → exit 0 = green, 1 = a fail.
"""
from __future__ import annotations
import json, sys
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
    # GONE board is included in assignments but has no board.json
    assigns = {"/x/AAA/board": 7891, "/x/BBB/board": 7893,
               "/x/CCC/board": 7892, "/x/GONE/board": 7894}

    def fake_title(path):
        return {"/x/AAA/board": "WorkBoard",
                "/x/BBB/board": "QuantifyMe — Work Board",
                "/x/CCC/board": None}.get(str(path))

    _present = {"/x/AAA/board", "/x/BBB/board", "/x/CCC/board"}  # GONE absent
    def fake_present(path):
        return str(path) in _present

    _healthy_ports = {7891, 7892}  # AAA + CCC up; BBB (7893) down
    def fake_port_healthy(port, timeout=0.4):
        return port in _healthy_ports

    cap = _Cap()
    h = make_handler(Path("/x/AAA/board"), 7891, cap)
    with mock.patch.object(pr, "assignments", lambda: assigns), \
         mock.patch.object(serve, "_port_healthy", fake_port_healthy), \
         mock.patch.object(serve, "_board_title_for", fake_title), \
         mock.patch.object(serve, "_board_present", fake_present):
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
    check(7894 not in by_port, "GONE board (no board.json) omitted from /boards")


def test_real_helpers():
    import tempfile, json as _json
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path as _P
        d = _P(tmp)

        # _board_present: dir with board.json → True
        (d / "board.json").write_text("{}")
        check(serve._board_present(d) is True, "real _board_present: dir with board.json → True")

        # _board_present: dir without board.json → False
        d2 = _P(tmp) / "empty"
        d2.mkdir()
        check(serve._board_present(d2) is False, "real _board_present: dir without board.json → False")

        # _board_title_for: title "Foo" → "Foo"
        d3 = _P(tmp) / "titled"
        d3.mkdir()
        (d3 / "board.json").write_text(_json.dumps({"title": "Foo"}))
        check(serve._board_title_for(d3) == "Foo", 'real _board_title_for: "title":"Foo" → "Foo"')

        # _board_title_for: blank title → "" (raw, not None)
        d4 = _P(tmp) / "blank"
        d4.mkdir()
        (d4 / "board.json").write_text(_json.dumps({"title": ""}))
        check(serve._board_title_for(d4) == "", 'real _board_title_for: "title":"" → "" (raw, not None)')

        # _board_title_for: no title key → None
        d5 = _P(tmp) / "notitle"
        d5.mkdir()
        (d5 / "board.json").write_text(_json.dumps({}))
        check(serve._board_title_for(d5) is None, "real _board_title_for: no title key → None")

        # _board_title_for: missing dir → None
        check(serve._board_title_for(_P(tmp) / "nonexistent") is None,
              "real _board_title_for: missing dir → None")


if __name__ == "__main__":
    test()
    test_real_helpers()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
