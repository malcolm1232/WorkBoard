#!/usr/bin/env python3
"""#858 code-review findings 3, 4, 10, 5 — probe/reassign hardening.

  F3.  200 JSON with NO 'board' key and NO board-server signature (a generic
       dev service squatting the port) must read "wrong", not permissive "ok".
  F3b. The genuine auth-trimmed payload (#842: ok/rev/cards/sseClients, no
       board path) must STAY permissive — we can't verify, don't flap.
  F4.  A squatter answering {"board": 123} must read "wrong", not raise
       TypeError through _probe_board (which killed the whole /boards reply).
  F10. One malformed registry value must not collapse the taken-set: the
       reassign scan may never hand out another board's designated port.
  F5.  Reassignment is a locked RMW in port_registry (TOCTOU #633 class):
       concurrent reassigns for different boards get DISTINCT ports and both
       land in the assignments file.

Run: python3 dev/test_858_probe_hardening.py  → exit 0 = green.
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# Isolate every registry write BEFORE importing the modules under test.
_STATE = Path(tempfile.mkdtemp(prefix="t858hard-"))
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


def stub_json(payload: dict):
    body = json.dumps(payload).encode()
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]

def mk_board(name):
    root = Path(tempfile.mkdtemp(prefix=f"t858hard-{name}-"))
    bd = root / "board"; bd.mkdir()
    (bd / "board.json").write_text("{}")
    return str(bd.resolve())


def f3_generic_service_is_wrong():
    print("F3: generic no-'board' JSON is positive squatter evidence")
    srv, port = stub_json({"status": "healthy", "uptime": 12345})
    try:
        state = serve._probe_board(port, expect_board="/x/board")
        check(state == "wrong", f"generic health JSON → wrong (got {state!r})")
    finally:
        srv.shutdown()


def f3b_auth_trimmed_stays_permissive():
    print("F3b: genuine auth-trimmed board payload stays permissive")
    srv, port = stub_json({"ok": True, "rev": 5, "cards": 2, "sseClients": 0,
                           "lastSseConnectMs": 0, "nowMs": 1, "ts": "t"})
    try:
        state = serve._probe_board(port, expect_board="/x/board")
        check(state == "ok", f"auth-trimmed payload → ok (got {state!r})")
    finally:
        srv.shutdown()


def f4_non_string_board_is_wrong_not_crash():
    print("F4: {'board': 123} → wrong, never an exception")
    srv, port = stub_json({"board": 123})
    try:
        try:
            state = serve._probe_board(port, expect_board="/x/board")
        except Exception as e:
            state = f"RAISED {type(e).__name__}"
        check(state == "wrong", f"non-string board → wrong (got {state!r})")
    finally:
        srv.shutdown()


def f10_malformed_value_does_not_collapse_taken():
    print("F10: malformed registry value doesn't free other boards' ports")
    bd_new = mk_board("f10new")
    bd_down = mk_board("f10down")   # a DOWN board that owns a port
    down_port = 7894                # free on the box, but DESIGNATED
    Path(os.environ["BOARD_ASSIGNMENTS"]).write_text(json.dumps(
        {bd_down: down_port, mk_board("f10bad"): "garbage"}))
    got = serve._reassign_squatted(bd_new, old_port=7999)
    check(got is not None and got != down_port,
          f"scan skipped the down board's designated {down_port} (got {got})")
    a = json.loads(Path(os.environ["BOARD_ASSIGNMENTS"]).read_text())
    check(a.get(bd_down) == down_port, "down board kept its designation")
    check(a.get(bd_new) == got, "new designation persisted")


def f5_concurrent_reassigns_get_distinct_ports():
    print("F5: concurrent reassigns are serialized (locked RMW)")
    boards = [mk_board(f"f5-{i}") for i in range(4)]
    Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")
    code = ("import sys; sys.path.insert(0, sys.argv[2]); import port_registry as pr; "
            "print(pr.reassign(sys.argv[1], 7999))")
    procs = [subprocess.Popen([sys.executable, "-c", code, b, str(REPO / "scripts")],
                              env=dict(os.environ), stdout=subprocess.PIPE, text=True)
             for b in boards]
    ports = [p.communicate(timeout=30)[0].strip() for p in procs]
    check(all(o.isdigit() for o in ports), f"all reassigns returned ports ({ports})")
    check(len(set(ports)) == len(ports), f"all ports DISTINCT ({ports})")
    a = json.loads(Path(os.environ["BOARD_ASSIGNMENTS"]).read_text())
    check(all(str(a.get(b, "")) == o for b, o in zip(boards, ports)),
          "every board's designation landed in the file (no lost update)")


if __name__ == "__main__":
    t0 = time.time()
    f3_generic_service_is_wrong()
    f3b_auth_trimmed_stays_permissive()
    f4_non_string_board_is_wrong_not_crash()
    f10_malformed_value_does_not_collapse_taken()
    f5_concurrent_reassigns_get_distinct_ports()
    print(f"{'PASS' if _fails == 0 else f'FAIL ({_fails})'}  ({time.time()-t0:.1f}s)")
    sys.exit(1 if _fails else 0)
