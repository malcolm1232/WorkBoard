#!/usr/bin/env python3
"""#856 review CRITICAL 1 - real two-process cross-board SSE fanout.

dev/test_856_e2e.py asserts board B's state via a FRESH GET /inbox after a
claim on board A - that re-reads the shared inbox file directly and never
exercises the SSE push path at all, so it kept passing while a claim on one
board left the item rendered on every OTHER OPEN board indefinitely. The
README promises the item "disappears from every other board the instant
it's claimed" - that was only true once _inbox.notify_boards() got wired
into _handle_inbox_claim / _handle_inbox_discard (serve.py).

This test stands up TWO REAL board-steward server PROCESSES on two real
ports, subscribes to board B's actual /events SSE stream, claims an item on
board A over real HTTP, and asserts board B's SSE stream receives an
inbox-updated event whose payload no longer carries the claimed tid - the
one observable behavior the feature actually promises. It also covers
discard the same way.

Fully hermetic: isolated temp inbox/registry/assignments/telegram-config,
temp board dirs, real ports found by probing for anything ACTUALLY free
(never assumed free just because an isolated assignments file says so) so
it can't collide with a real running board. The user's live board and real
inbox are snapshotted before and asserted byte-for-byte untouched after.

Run: python3 dev/test_856_cross_board_sse.py
"""
from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

_STATE = Path(tempfile.mkdtemp(prefix="t856ssecross-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")
Path(os.environ["BOARD_REGISTRY"]).write_text("{}")
os.environ.pop("BOARD_AUTH_TOKEN", None)  # no auth gate for this harness

LIVE_BOARD = Path.home() / "Desktop" / "WorkBoard" / "board" / "board.json"
LIVE_BEFORE = LIVE_BOARD.read_text() if LIVE_BOARD.exists() else None
LIVE_INBOX = Path.home() / ".board-steward" / "inbox.jsonl"
LIVE_INBOX_BEFORE = LIVE_INBOX.read_text() if LIVE_INBOX.exists() else None

import _inbox  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def _free_port(lo=7891, hi=7999, exclude=()):
    """A port nothing is ACTUALLY listening on right now. Never assumed free
    just because it's unclaimed in the isolated (temp) assignments file - a
    real board, or anything else, may genuinely be bound to it on this
    machine. Same connect_ex probe port_registry/serve use for their own
    liveness checks."""
    for p in range(lo, hi + 1):
        if p in exclude:
            continue
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError(f"no free port in [{lo}, {hi}]")


def mk_board(name):
    bd = Path(tempfile.mkdtemp(prefix=f"t856ssecross-{name}-")) / "board"
    bd.mkdir(parents=True)
    (bd / "board.json").write_text(json.dumps({
        "title": name, "rev": 1, "nextNum": 1, "schemaVersion": 3,
        "columns": [{"id": "task", "name": "Task"}, {"id": "notes", "name": "Notes"}],
        "cards": [], "activeWork": None,
    }))
    return bd


def spawn_server(board_dir: Path, port: int) -> subprocess.Popen:
    log = board_dir / "server.log"
    return subprocess.Popen(
        [sys.executable, str(SCRIPTS / "serve.py"),
         "--board", str(board_dir / "board.json"), "--port", str(port)],
        stdout=open(log, "wb"), stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=dict(os.environ),
    )


def wait_healthy(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def sse_reader(port: int, out_queue: "queue.Queue", stop_event: threading.Event,
               connected_event: threading.Event) -> None:
    """Reads /events for `port`, parses SSE frames, and pushes (name, data)
    tuples for every REAL event onto out_queue (comments/keepalives are
    swallowed, but a ": connected" comment sets connected_event)."""
    url = f"http://127.0.0.1:{port}/events"
    try:
        resp = urllib.request.urlopen(url, timeout=2)
    except Exception as e:
        out_queue.put(("__error__", str(e)))
        return
    buf = b""
    try:
        while not stop_event.is_set():
            try:
                # read1(), NOT read(): HTTPResponse.read(n) is a BUFFERED read
                # that blocks trying to fill exactly n bytes, which never
                # happens for a small SSE frame (e.g. the 14-byte ": connected
                # \n\n") until either more data arrives or the socket times
                # out. read1() returns as soon as ANY data is available from
                # one underlying recv, which is what a live stream needs.
                chunk = resp.read1(1024)
            except (socket.timeout, TimeoutError):
                continue
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                text = raw.decode("utf-8", "replace")
                if not text or text.startswith(": "):
                    if text.startswith(": connected"):
                        connected_event.set()
                    continue
                name = data = None
                for line in text.splitlines():
                    if line.startswith("event: "):
                        name = line[len("event: "):]
                    elif line.startswith("data: "):
                        data = line[len("data: "):]
                if name:
                    out_queue.put((name, data))
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _wait_for_event(events: "queue.Queue", name_wanted: str, deadline: float):
    while time.time() < deadline:
        try:
            name, data = events.get(timeout=max(0.05, deadline - time.time()))
        except queue.Empty:
            return None
        if name == name_wanted:
            return json.loads(data)
    return None


def main():
    print("real two-process cross-board SSE fanout")
    port_a = _free_port()
    port_b = _free_port(exclude={port_a})
    board_a = mk_board("a")
    board_b = mk_board("b")

    it1 = _inbox.append("https://ex.com/cross-board-claim-check", update_id=9001)
    it2 = _inbox.append("cross-board discard check", update_id=9002)
    check(it1 is not None and it2 is not None, "captured two test items in the shared inbox")

    proc_a = spawn_server(board_a, port_a)
    proc_b = spawn_server(board_b, port_b)
    try:
        check(wait_healthy(port_a), f"board A came up on {port_a}")
        check(wait_healthy(port_b), f"board B came up on {port_b}")

        events = queue.Queue()
        stop_event = threading.Event()
        connected_event = threading.Event()
        reader = threading.Thread(
            target=sse_reader, args=(port_b, events, stop_event, connected_event), daemon=True)
        reader.start()
        check(connected_event.wait(timeout=5), "board B's SSE connection is live")
        time.sleep(0.2)  # let _handle_sse finish registering the client in _clients

        # Sanity: before the claim, board B's own GET /inbox still shows both items.
        with urllib.request.urlopen(f"http://127.0.0.1:{port_b}/inbox", timeout=3) as r:
            before = json.loads(r.read())
        before_tids = {i["tid"] for i in before["items"]}
        check({it1["tid"], it2["tid"]} <= before_tids,
              "board B shows both captures before anything is claimed/discarded")

        # ---- CLAIM on board A -> board B's SSE stream must update. ----
        body = json.dumps({"tid": it1["tid"], "column": "task"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port_a}/inbox/claim", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            claim_resp = json.loads(r.read())
        check(claim_resp.get("ok") is True, f"claim on board A succeeded ({claim_resp})")

        seen = _wait_for_event(events, "inbox-updated", time.time() + 10)
        check(seen is not None,
              "board B's SSE stream received an inbox-updated event after the claim on board A")
        if seen is not None:
            tids = [i["tid"] for i in seen.get("items", [])]
            check(it1["tid"] not in tids,
                  f"the pushed payload no longer carries the claimed tid (got {tids})")
            check(it2["tid"] in tids,
                  f"the still-unclaimed item is still in the pushed payload (got {tids})")

        # Board B never actually got a card: claiming is exclusive.
        db = json.loads((board_b / "board.json").read_text())
        check(db["cards"] == [], "board B never got a card - claiming is exclusive")
        da = json.loads((board_a / "board.json").read_text())
        check(len(da["cards"]) == 1, "board A really has the card on disk")

        # ---- DISCARD on board A -> board B's SSE stream must update too. ----
        body2 = json.dumps({"tid": it2["tid"]}).encode()
        req2 = urllib.request.Request(
            f"http://127.0.0.1:{port_a}/inbox/discard", data=body2,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req2, timeout=5) as r:
            discard_resp = json.loads(r.read())
        check(discard_resp.get("ok") is True, f"discard on board A succeeded ({discard_resp})")

        seen2 = _wait_for_event(events, "inbox-updated", time.time() + 10)
        check(seen2 is not None,
              "board B's SSE stream received a second inbox-updated event after the discard")
        if seen2 is not None:
            tids2 = [i["tid"] for i in seen2.get("items", [])]
            check(it2["tid"] not in tids2,
                  f"the discarded item is gone from the pushed payload too (got {tids2})")

        stop_event.set()
    finally:
        for p in (proc_a, proc_b):
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    if LIVE_BEFORE is not None:
        check(LIVE_BOARD.read_text() == LIVE_BEFORE, "the LIVE board is untouched")
    now_inbox = LIVE_INBOX.read_text() if LIVE_INBOX.exists() else None
    check(now_inbox == LIVE_INBOX_BEFORE, "the REAL inbox is untouched")


if __name__ == "__main__":
    main()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
