#!/usr/bin/env python3
"""#841: POST /ensure-board health-checks a project's port, spawns it if down,
returns {port,url}. Rejects unknown paths (no arbitrary spawn).

Run: python3 dev/test_841_ensure_board.py  → exit 0 = green, 1 = a fail.
"""
from __future__ import annotations
import json, os, sys, tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# Isolate registry writes (#858: reassign persists via port_registry.reassign).
_STATE = Path(tempfile.mkdtemp(prefix="t841-"))
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
os.environ["BOARD_ACTIVE"] = str(_STATE / "last-active")

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
         mock.patch.object(serve, "_probe_board", lambda port, **kw: "ok"), \
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
    spawned = {"n": 0}
    def fake_spawn(board_dir, port): spawned["n"] += 1; return True
    # A down port is "unreachable" (both the fast probe and the generous
    # confirming one) → spawn on the SAME port, no registry rewrite.
    with mock.patch.object(pr, "assignments", lambda: ASSIGNS), \
         mock.patch.object(serve, "_probe_board", lambda port, **kw: "unreachable"), \
         mock.patch.object(serve, "_spawn_board", fake_spawn):
        h._handle_ensure_board()
    check(cap.status == 200, "spawn path returns 200")
    check(spawned["n"] == 1, "spawned exactly once")
    check(json.loads(cap.body)["port"] == 7891, "returns the spawned port")


def test_spawn_fails_504():
    cap = _Cap()
    h = make_handler("/x/AAA/board", cap)
    with mock.patch.object(pr, "assignments", lambda: ASSIGNS), \
         mock.patch.object(serve, "_probe_board", lambda port, **kw: "unreachable"), \
         mock.patch.object(serve, "_spawn_board", lambda board_dir, port: False):
        h._handle_ensure_board()
    check(cap.status == 504, "spawn failure returns 504")


def test_squatted_port_reassigns():
    """#858 — the designated port ANSWERS /health but serves a DIFFERENT board
    (stale server after a project move): probe state "wrong", the ONLY state
    allowed to move the designation (explicit set_port) and spawn fresh —
    never route the tab to the squatter, never try to bind over it. Mere
    unreachability must NOT land here (see dev/test_858_transient_timeout.py)."""
    # Real temp board dirs: port_registry.reassign GCs designations whose dir
    # is gone, so fake /x/ paths would vanish from the file it rewrites.
    aaa = Path(tempfile.mkdtemp(prefix="t841-aaa-")) / "board"; aaa.mkdir()
    bbb = Path(tempfile.mkdtemp(prefix="t841-bbb-")) / "board"; bbb.mkdir()
    aaa, bbb = str(aaa.resolve()), str(bbb.resolve())
    assigns = {aaa: 7891, bbb: 7893}
    Path(os.environ["BOARD_ASSIGNMENTS"]).write_text(json.dumps(assigns))
    cap = _Cap()
    h = make_handler(aaa, cap)
    spawned = {}
    def fake_spawn(board_dir, port): spawned["port"] = port; return True
    with mock.patch.object(serve, "_probe_board", lambda port, **kw: "wrong"), \
         mock.patch.object(serve, "_port_in_use", lambda port: port == 7891), \
         mock.patch.object(serve, "_spawn_board", fake_spawn):
        h._handle_ensure_board()
    check(cap.status == 200, "squatted port returns 200")
    new_port = json.loads(cap.body)["port"]
    check(new_port != 7891, "did not return the squatted port")
    check(new_port not in assigns.values(), "new port avoids other designations")
    after = json.loads(Path(os.environ["BOARD_ASSIGNMENTS"]).read_text())
    check(after.get(aaa) == new_port, "designation moved in the assignments file")
    check(spawned.get("port") == new_port, "spawned on the NEW port")


if __name__ == "__main__":
    test_unknown_path(); test_already_up(); test_spawn_when_down(); test_spawn_fails_504()
    test_squatted_port_reassigns()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
