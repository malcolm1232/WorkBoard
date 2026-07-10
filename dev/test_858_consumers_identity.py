#!/usr/bin/env python3
"""#858 code-review finding 2: the identity check must live in the shared probe
layer, not just /ensure-board. Exercises the OTHER consumers end-to-end with
real processes and an isolated registry (BOARD_REGISTRY/ASSIGNMENTS/ACTIVE):

  C1. serve.py startup, designated port held by a FOREIGN board server
      -> must move the designation and come up on a fresh port
         (old behavior: bind fail -> exit 1 -> launchd flap, forever).
  C2. serve.py startup, designated port held by a SLOW server for the SAME
      board (its /health can stall ~2s on git; the 0.5s guard probe misses)
      -> must exit 0 as a duplicate (old behavior: bind fail -> exit 1).
  C3. card.py board-new, existing project, designated port squatted
      -> must NOT report the squatter as "already running"; ends up serving
         the board on a fresh port.

Run: python3 dev/test_858_consumers_identity.py  → exit 0 = green.
"""
from __future__ import annotations
import json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVE = str(REPO / "scripts" / "serve.py")
CARD = str(REPO / "scripts" / "card.py")

_fails = 0
def check(cond, msg):
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond: _fails += 1

def isolated_env(state: Path):
    env = dict(os.environ,
               BOARD_REGISTRY=str(state / "registry.json"),
               BOARD_ASSIGNMENTS=str(state / "assignments.json"),
               BOARD_ACTIVE=str(state / "last-active"),
               BOARD_NO_AUTO_OPEN="1")
    env.pop("CLAUDECODE", None)
    return env

def mk_project(name, parent=None):
    root = Path(parent or tempfile.mkdtemp(prefix=f"e2e858c-{name}-"))
    if parent: root = Path(parent) / name; root.mkdir()
    bd = root / "board"; bd.mkdir(parents=True, exist_ok=True)
    (bd / "board.json").write_text(json.dumps({
        "title": name, "revision": 1,
        "columns": [{"id": "task", "title": "Task", "cards": []}]}))
    return root.resolve(), str(bd.resolve())

def stub(board_path, delay):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(delay)
            body = json.dumps({"board": board_path, "sseClients": 0}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]

def health(port, timeout=2):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
        return json.loads(r.read())

def wait_for(pred, secs=12):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            v = pred()
            if v: return v
        except Exception: pass
        time.sleep(0.3)
    return None

def kill_from_registry(state: Path, port):
    try:
        reg = json.loads((state / "registry.json").read_text())
        for v in reg.values():
            if v.get("port") == port and v.get("pid"):
                os.kill(int(v["pid"]), 15)
    except Exception: pass


def c1_startup_relocates_off_foreign_holder():
    print("C1: serve.py startup vs FOREIGN holder on designated port")
    state = Path(tempfile.mkdtemp(prefix="e2e858c-s1-"))
    root, bd = mk_project("c1proj")
    srv, port = stub("/somewhere/else/board", delay=0.0)
    (state / "assignments.json").write_text(json.dumps({bd: port}))
    p = subprocess.Popen([sys.executable, SERVE, "--project", str(root)],
                         env=isolated_env(state),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    new_port = None
    try:
        def moved():
            a = json.loads((state / "assignments.json").read_text())
            np = a.get(bd)
            return np if np and np != port else None
        new_port = wait_for(moved)
        check(new_port is not None, "designation moved off the squatted port")
        h = wait_for(lambda: health(new_port)) if new_port else None
        check(bool(h) and h.get("board") == bd,
              f"server came up on new port {new_port} serving the right board")
        check(p.poll() is None, "server process alive (no exit-1 launchd flap)")
    finally:
        p.terminate(); srv.shutdown()
        try: p.wait(timeout=5)
        except Exception: p.kill()


def c2_startup_clean_exit_on_slow_same_board():
    print("C2: serve.py startup vs SLOW server for the SAME board")
    state = Path(tempfile.mkdtemp(prefix="e2e858c-s2-"))
    root, bd = mk_project("c2proj")
    srv, port = stub(bd, delay=1.2)   # same board, /health stalls past 0.5s
    (state / "assignments.json").write_text(json.dumps({bd: port}))
    p = subprocess.run([sys.executable, SERVE, "--project", str(root)],
                       env=isolated_env(state), capture_output=True,
                       text=True, timeout=30)
    srv.shutdown()
    check(p.returncode == 0,
          f"exits 0 as a duplicate (got rc={p.returncode})")
    check("duplicate" in p.stderr, "says it's not starting a duplicate")
    a = json.loads((state / "assignments.json").read_text())
    check(a.get(bd) == port, "designation NOT moved (server is ours, just slow)")


def c3_board_new_not_fooled_by_squatter():
    print("C3: card.py board-new vs squatter on the designated port")
    state = Path(tempfile.mkdtemp(prefix="e2e858c-s3-"))
    parent = Path(tempfile.mkdtemp(prefix="e2e858c-s3p-"))
    root, bd = mk_project("c3proj", parent=parent)
    srv, port = stub("/somewhere/else/board", delay=0.0)
    (state / "assignments.json").write_text(json.dumps({bd: port}))
    p = subprocess.run([sys.executable, CARD, "board-new", "c3proj",
                        "--dir", str(parent)],
                       env=isolated_env(state), capture_output=True,
                       text=True, timeout=40)
    out = p.stdout + p.stderr
    new_port = None
    try:
        check(f"already running → http://127.0.0.1:{port}" not in out,
              "does NOT report the squatter as already running")
        a = json.loads((state / "assignments.json").read_text())
        new_port = a.get(bd)
        check(new_port and new_port != port,
              f"board ended up designated off the squatted port ({new_port})")
        h = wait_for(lambda: health(new_port)) if new_port else None
        check(bool(h) and h.get("board") == bd,
              "a real server on the new port serves the right board")
    finally:
        srv.shutdown()
        if new_port: kill_from_registry(state, new_port)


def h1_session_hook_not_fooled_by_squatter():
    print("H1: hook_session_start.sh vs squatter on the designated port")
    state = Path(tempfile.mkdtemp(prefix="e2e858c-h1-"))
    home = Path(tempfile.mkdtemp(prefix="e2e858c-h1home-"))
    (home / ".board-steward").mkdir()
    (home / ".board-steward" / ".onboarded").touch()
    root, bd = mk_project("h1proj")
    srv, port = stub("/somewhere/else/board", delay=0.0)
    (state / "assignments.json").write_text(json.dumps({bd: port}))
    env = isolated_env(state)
    env["HOME"] = str(home)
    p = subprocess.run(["bash", str(REPO / "scripts" / "hook_session_start.sh")],
                       input='{"session_id":"h1-e2e","source":"startup"}',
                       cwd=str(root), env=env, capture_output=True,
                       text=True, timeout=90)
    out = p.stdout + p.stderr
    new_port = None
    try:
        check(f"Live at http://127.0.0.1:{port}" not in out,
              "digest does NOT route the session to the squatter")
        a = json.loads((state / "assignments.json").read_text())
        new_port = a.get(bd)
        check(new_port and new_port != port,
              f"designation moved off the squatted port ({new_port})")
        h = wait_for(lambda: health(new_port)) if new_port else None
        check(bool(h) and h.get("board") == bd,
              "hook spawned a real server for the right board on the new port")
        if f"Live at http://127.0.0.1:" in out:
            check(f"Live at http://127.0.0.1:{new_port}" in out,
                  "the Live digest line names the NEW port")
    finally:
        srv.shutdown()
        if new_port: kill_from_registry(state, new_port)


if __name__ == "__main__":
    t0 = time.time()
    c1_startup_relocates_off_foreign_holder()
    c2_startup_clean_exit_on_slow_same_board()
    c3_board_new_not_fooled_by_squatter()
    h1_session_hook_not_fooled_by_squatter()
    print(f"{'PASS' if _fails == 0 else f'FAIL ({_fails})'}  ({time.time()-t0:.1f}s)")
    sys.exit(1 if _fails else 0)
