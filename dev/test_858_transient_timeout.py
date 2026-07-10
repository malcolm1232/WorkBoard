#!/usr/bin/env python3
"""#858 code-review critical finding: a TRANSIENT /health stall on a healthy,
correct board server must never be misdiagnosed as a squatter. The old logic
(`not _port_healthy` + `_port_in_use` => reassign) permanently rewrote the port
registry and spawned a DUPLICATE server against the same board.json — two live
writers — off a single 0.4s probe timeout, which the server's own git-heavy
/health (~2s worst case) can exceed under load.

Contract under test (ensure-board decision):
  - slow-but-CORRECT server  -> confirm with a generous timeout, keep the port,
                                NO set_port, NO spawn (probes are REAL here).
  - positively WRONG board   -> reassign + spawn on the new port (the genuine
                                #858 squatter case must keep working).
  - unreachable-but-occupied -> NO reassign (no positive evidence); fall back to
                                a same-port spawn attempt -> 504 if it can't.

Run: python3 dev/test_858_transient_timeout.py  → exit 0 = green, 1 = a fail.
"""
from __future__ import annotations
import json
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def make_handler(path_qs, cap):
    h = serve.BoardHandler.__new__(serve.BoardHandler)
    h.path = f"/ensure-board?path={path_qs}"
    h._send = cap
    h.headers = {"Content-Length": "0"}
    h.rfile = None
    return h


def _health_server(board_path: str, delay: float):
    """Real HTTP server whose /health answers 200 {"board": board_path} after
    `delay` seconds — the shape of a healthy board server under load."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(delay)
            body = json.dumps({"board": board_path, "sseClients": 0}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # probe already gave up — expected for the timed-out one
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_slow_correct_server_is_not_a_squatter():
    """THE critical case: /health is slow (1.2s > the 0.4s probe) but the
    server is the RIGHT board. Real probes, no probe mocks."""
    board_dir = str(Path(tempfile.mkdtemp()) / "board"); Path(board_dir).mkdir()
    srv, port = _health_server(board_dir, delay=1.2)
    try:
        cap = _Cap()
        h = make_handler(board_dir, cap)
        reassigned, spawned = {}, {"n": 0}
        def fake_spawn(*a, **k): spawned["n"] += 1; return True
        with mock.patch.object(pr, "assignments", lambda: {board_dir: port}), \
             mock.patch.object(pr, "set_port",
                               lambda bd, p: reassigned.update({str(bd): p})), \
             mock.patch.object(serve, "_spawn_board", fake_spawn):
            h._handle_ensure_board()
        check(reassigned == {}, "slow-but-correct server: registry NOT rewritten")
        check(spawned["n"] == 0, "slow-but-correct server: NO duplicate spawn")
        check(cap.status == 200, "returns 200")
        check(cap.status == 200 and json.loads(cap.body)["port"] == port,
              "returns the ORIGINAL port")
    finally:
        srv.shutdown()


def test_wrong_board_still_reassigns():
    """Regression guard for the genuine #858 squatter: fast 200 with a
    DIFFERENT board path must still move the designation and spawn fresh."""
    board_dir = str(Path(tempfile.mkdtemp()) / "board"); Path(board_dir).mkdir()
    other_dir = str(Path(tempfile.mkdtemp()) / "board"); Path(other_dir).mkdir()
    srv, port = _health_server(other_dir, delay=0.0)   # answers as the WRONG board
    try:
        cap = _Cap()
        h = make_handler(board_dir, cap)
        reassigned, spawned = {}, {}
        with mock.patch.object(pr, "assignments", lambda: {board_dir: port}), \
             mock.patch.object(pr, "set_port",
                               lambda bd, p: reassigned.update({str(bd): p})), \
             mock.patch.object(serve, "_spawn_board",
                               lambda bd, p: spawned.update({"port": p}) or True):
            h._handle_ensure_board()
        check(cap.status == 200, "real squatter: returns 200")
        new_port = json.loads(cap.body)["port"] if cap.status == 200 else None
        check(new_port is not None and new_port != port,
              "real squatter: designation moved off the squatted port")
        check(reassigned.get(board_dir) == new_port,
              "real squatter: registry rewritten via set_port")
        check(spawned.get("port") == new_port, "real squatter: spawned on NEW port")
    finally:
        srv.shutdown()


def test_hung_listener_never_reassigns():
    """A listener that accepts but never answers HTTP: no positive identity
    evidence -> NO registry rewrite. Same-port spawn attempt fails -> 504."""
    board_dir = str(Path(tempfile.mkdtemp()) / "board"); Path(board_dir).mkdir()
    lsock = socket.socket(); lsock.bind(("127.0.0.1", 0)); lsock.listen(5)
    port = lsock.getsockname()[1]
    try:
        cap = _Cap()
        h = make_handler(board_dir, cap)
        reassigned = {}
        with mock.patch.object(pr, "assignments", lambda: {board_dir: port}), \
             mock.patch.object(pr, "set_port",
                               lambda bd, p: reassigned.update({str(bd): p})), \
             mock.patch.object(serve, "_spawn_board", lambda bd, p: False):
            h._handle_ensure_board()
        check(reassigned == {}, "hung listener: registry NOT rewritten")
        check(cap.status == 504, "hung listener: 504 (spawn blocked), not a reassign")
    finally:
        lsock.close()


if __name__ == "__main__":
    t0 = time.time()
    test_slow_correct_server_is_not_a_squatter()
    test_wrong_board_still_reassigns()
    test_hung_listener_never_reassigns()
    print(f"{'PASS' if _fails == 0 else f'FAIL ({_fails})'}  ({time.time()-t0:.1f}s)")
    sys.exit(1 if _fails else 0)
