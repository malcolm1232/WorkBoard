#!/usr/bin/env python3
"""#841: POST /ensure-board health-checks a project's port, spawns it if down,
returns {port,url}. Rejects unknown paths (no arbitrary spawn).

Run: python3 dev/test_841_ensure_board.py  → exit 0 = green, 1 = a fail.
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
    def __init__(self): self.status = None; self.body = b""
    def __call__(self, status, body, ctype="application/json", extra=None):
        self.status = status; self.body = body


def make_handler(path_qs, cap, content_length=0):
    h = serve.BoardHandler.__new__(serve.BoardHandler)
    h.path = f"/ensure-board?path={path_qs}"
    h._send = cap
    h.headers = {"Content-Length": str(content_length)}
    h.rfile = None  # no body to drain in unit tests (content_length=0)
    return h


ASSIGNS = {"/x/AAA/board": 7891, "/x/BBB/board": 7893}


def test_unknown_path():
    cap = _Cap()
    h = make_handler("/x/EVIL/board", cap)
    with mock.patch.object(pr, "assignments", lambda: ASSIGNS):
        h._handle_ensure_board()
    check(cap.status == 400, "unknown path rejected 400")


def test_already_up():
    cap = _Cap()
    h = make_handler("/x/BBB/board", cap)
    spawned = {"n": 0}
    def fake_spawn(*a, **k): spawned["n"] += 1; return True
    with mock.patch.object(pr, "assignments", lambda: ASSIGNS), \
         mock.patch.object(serve, "_port_healthy", lambda port, **kw: True), \
         mock.patch.object(serve, "_spawn_board", fake_spawn):
        h._handle_ensure_board()
    check(cap.status == 200, "already-up returns 200")
    data = json.loads(cap.body)
    check(data["port"] == 7893, "returns the right port")
    check(data["url"] == "http://127.0.0.1:7893", "returns the url")
    check(spawned["n"] == 0, "did NOT spawn (already healthy)")


def test_spawn_when_down():
    cap = _Cap()
    h = make_handler("/x/AAA/board", cap)
    health = {"calls": 0}
    def fake_health(port, **kw):     # down first, up after spawn
        health["calls"] += 1
        return health["calls"] > 1
    spawned = {"n": 0}
    def fake_spawn(board_dir, port): spawned["n"] += 1; return True
    # _port_in_use MUST be faked: the real check hits live localhost ports (#858).
    with mock.patch.object(pr, "assignments", lambda: ASSIGNS), \
         mock.patch.object(serve, "_port_healthy", fake_health), \
         mock.patch.object(serve, "_port_in_use", lambda port: False), \
         mock.patch.object(serve, "_spawn_board", fake_spawn):
        h._handle_ensure_board()
    check(cap.status == 200, "spawn path returns 200")
    check(spawned["n"] == 1, "spawned exactly once")
    check(json.loads(cap.body)["port"] == 7891, "returns the spawned port")


def test_spawn_fails_504():
    cap = _Cap()
    h = make_handler("/x/AAA/board", cap)
    with mock.patch.object(pr, "assignments", lambda: ASSIGNS), \
         mock.patch.object(serve, "_port_healthy", lambda port, **kw: False), \
         mock.patch.object(serve, "_port_in_use", lambda port: False), \
         mock.patch.object(serve, "_spawn_board", lambda board_dir, port: False):
        h._handle_ensure_board()
    check(cap.status == 504, "spawn failure returns 504")


def test_squatted_port_reassigns():
    """#858 — the designated port ANSWERS /health but serves a DIFFERENT board
    (stale server after a project move). ensure-board must move the designation
    to a fresh port (explicit set_port) and spawn there — never route the tab
    to the squatter, never try to bind over it."""
    cap = _Cap()
    h = make_handler("/x/AAA/board", cap)
    spawned = {}
    def fake_spawn(board_dir, port): spawned["port"] = port; return True
    reassigned = {}
    def fake_set_port(board_dir, port): reassigned[str(board_dir)] = port
    with mock.patch.object(pr, "assignments", lambda: ASSIGNS), \
         mock.patch.object(pr, "set_port", fake_set_port), \
         mock.patch.object(serve, "_port_healthy", lambda port, **kw: False), \
         mock.patch.object(serve, "_port_in_use", lambda port: port == 7891), \
         mock.patch.object(serve, "_spawn_board", fake_spawn):
        h._handle_ensure_board()
    check(cap.status == 200, "squatted port returns 200")
    new_port = json.loads(cap.body)["port"]
    check(new_port != 7891, "did not return the squatted port")
    check(new_port not in ASSIGNS.values(), "new port avoids other designations")
    check(reassigned.get("/x/AAA/board") == new_port, "designation moved via set_port")
    check(spawned.get("port") == new_port, "spawned on the NEW port")


if __name__ == "__main__":
    test_unknown_path(); test_already_up(); test_spawn_when_down(); test_spawn_fails_504()
    test_squatted_port_reassigns()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
