#!/usr/bin/env python3
"""board-steward local server — serves board.html + board.json + live SSE events.

Default: bind 127.0.0.1:7891, serve the board found at <cwd>/board/board.json
(walks up parent dirs if not found). board.html is served from the same
directory; if missing, falls back to the skill's bundled template.

v4: adds /events Server-Sent Events stream. Every POST /board.json is diffed
against the prior state in memory; per-card / per-column changes are broadcast
to all connected EventSource clients as named events (card-added,
card-updated, card-removed, column-added, column-removed, column-renamed,
rev-bumped). The browser animates the diff incrementally instead of
re-rendering the whole board on poll.

Stdlib only — no pip deps.

Usage:
    python serve.py                       # cwd, port 7891
    python serve.py --port 8080
    python serve.py --project /path/to/project
    python serve.py --board /explicit/path/to/board.json
    python serve.py --bootstrap           # create board/ if missing
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_HTML = SKILL_DIR / "templates" / "board.html"
TEMPLATE_JSON = SKILL_DIR / "templates" / "board.json"
REGEN_SCRIPT = SKILL_DIR / "scripts" / "regen_index.py"

_write_lock = threading.Lock()
_clients_lock = threading.Lock()
_clients: list[queue.Queue] = []
_cached_state: dict | None = None
_cached_lock = threading.Lock()


# ===== state I/O =====

def find_board_dir(start: Path) -> Path | None:
    """Walk up from start looking for board/board.json. Return board/ dir."""
    cur = start.resolve()
    for _ in range(8):
        candidate = cur / "board" / "board.json"
        if candidate.is_file():
            return cur / "board"
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


TAG_PROFILES = SKILL_DIR / "templates" / "tag-profiles.json"

def _load_tag_profile(profile: str) -> dict:
    """Read tag-profiles.json and return one profile's main+sub. Falls back to
    software if the requested profile doesn't exist."""
    if not TAG_PROFILES.exists():
        return {"profile": profile, "main": [], "sub": []}
    profiles = json.loads(TAG_PROFILES.read_text())
    chosen = profiles.get(profile) or profiles.get("software") or {}
    return {
        "profile": profile if profile in profiles else "software",
        "main": chosen.get("main", []),
        "sub":  chosen.get("sub", []),
    }


def bootstrap_board(board_dir: Path, profile: str = "software") -> None:
    """First-run init: create board/ with starter board.json + board.html.
    Auto-fills `title` from the project directory basename and seeds a tag
    taxonomy from the chosen industry profile so cards have semantic, color-
    coded tags from day one."""
    board_dir.mkdir(parents=True, exist_ok=True)
    target_json = board_dir / "board.json"
    if not target_json.exists() and TEMPLATE_JSON.exists():
        data = json.loads(TEMPLATE_JSON.read_text())
        project_name = board_dir.parent.name or "WorkBoard"
        data["title"] = f"WorkBoard — {project_name}"
        data["tagTaxonomy"] = _load_tag_profile(profile)
        target_json.write_text(json.dumps(data, indent=2))
    target_html = board_dir / "board.html"
    if not target_html.exists() and TEMPLATE_HTML.exists():
        target_html.write_text(TEMPLATE_HTML.read_text())


def atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".board.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def regen_index(board_dir: Path) -> None:
    if not REGEN_SCRIPT.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(REGEN_SCRIPT), str(board_dir / "board.json")],
            timeout=10, check=False, capture_output=True,
        )
    except Exception:
        pass


# ===== diff: prev state → new state → list of named events =====

def diff_states(old: dict | None, new: dict) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    old = old or {"cards": [], "columns": []}

    old_cols = {c["id"]: c for c in old.get("columns", [])}
    new_cols = {c["id"]: c for c in new.get("columns", [])}
    new_col_order = [c["id"] for c in new.get("columns", [])]

    for idx, cid in enumerate(new_col_order):
        col = new_cols[cid]
        if cid not in old_cols:
            events.append(("column-added", {"column": col, "index": idx}))
        elif old_cols[cid].get("name") != col.get("name"):
            events.append(("column-renamed", {"id": cid, "name": col.get("name")}))
    for cid in old_cols:
        if cid not in new_cols:
            events.append(("column-removed", {"id": cid}))

    old_cards = {c["id"]: c for c in old.get("cards", [])}
    new_cards = {c["id"]: c for c in new.get("cards", [])}
    for cid, c in new_cards.items():
        if cid not in old_cards:
            events.append(("card-added", {"card": c}))
        else:
            oc = old_cards[cid]
            if (
                oc.get("updatedAt") != c.get("updatedAt")
                or oc.get("column") != c.get("column")
                or oc.get("title") != c.get("title")
                or oc.get("priority") != c.get("priority")
                or oc.get("doneAt") != c.get("doneAt")
                or json.dumps(oc.get("tags") or [], sort_keys=True)
                   != json.dumps(c.get("tags") or [], sort_keys=True)
            ):
                events.append((
                    "card-updated",
                    {"card": c, "fromColumn": oc.get("column"), "toColumn": c.get("column")},
                ))
    for cid in old_cards:
        if cid not in new_cards:
            events.append(("card-removed", {"id": cid}))

    return events


# ===== SSE broadcast =====

def broadcast(name: str, data: dict) -> None:
    payload = (name, data)
    with _clients_lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)


class BoardHandler(BaseHTTPRequestHandler):
    board_dir: Path = None  # set by main()
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if args and len(args) >= 2:
            code = args[1]
            method = args[0].split()[0] if args[0] else "?"
            if code.startswith("2") and method == "GET":
                return
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "null")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self._send(404, b'{"error":"not found"}')
            return
        self._send(200, data, ctype)

    # ----- SSE -----
    def _handle_sse(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        q: queue.Queue = queue.Queue(maxsize=256)
        with _clients_lock:
            _clients.append(q)
        try:
            while True:
                try:
                    name, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                line = f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/events":
            self._handle_sse()
            return

        if path == "/" or path == "/board.html":
            html_path = self.board_dir / "board.html"
            if not html_path.exists():
                html_path = TEMPLATE_HTML
            self._send_file(html_path, "text/html; charset=utf-8")
            return

        if path == "/board.json":
            self._send_file(self.board_dir / "board.json", "application/json")
            return

        if path == "/index.json":
            idx = self.board_dir / "index.json"
            if not idx.exists():
                regen_index(self.board_dir)
            self._send_file(idx, "application/json")
            return

        if path == "/health":
            try:
                state = json.loads((self.board_dir / "board.json").read_text())
                rev = state.get("rev", 0)
                cards = len(state.get("cards", []))
            except Exception:
                rev, cards = -1, 0
            with _clients_lock:
                n_clients = len(_clients)
            # #177 — include current git commit (short SHA + first line of
            # message) so the Logs HUD can show "running fde639b" without a
            # separate round-trip. Best-effort; silent fail if not a repo.
            commit_sha, commit_msg = "", ""
            try:
                # Walk up from board_dir looking for a .git
                cur = self.board_dir.resolve()
                for _ in range(6):
                    if (cur / ".git").exists():
                        commit_sha = subprocess.check_output(
                            ["git", "-C", str(cur), "rev-parse", "--short", "HEAD"],
                            stderr=subprocess.DEVNULL, timeout=1
                        ).decode().strip()
                        commit_msg = subprocess.check_output(
                            ["git", "-C", str(cur), "log", "-1", "--pretty=%s"],
                            stderr=subprocess.DEVNULL, timeout=1
                        ).decode().strip()
                        break
                    if cur.parent == cur: break
                    cur = cur.parent
            except Exception:
                pass
            body = json.dumps({
                "ok": True,
                "project": str(self.board_dir.parent),
                "board": str(self.board_dir),
                "rev": rev,
                "cards": cards,
                "sseClients": n_clients,
                "commit": commit_sha,
                "commitMsg": commit_msg,
                "ts": datetime.now(timezone.utc).isoformat(),
            }).encode()
            self._send(200, body)
            return

        if path.startswith("/archive/"):
            rel = path[len("/archive/"):].lstrip("/")
            target = (self.board_dir / "archive" / rel).resolve()
            if not str(target).startswith(str((self.board_dir / "archive").resolve())):
                self._send(403, b'{"error":"forbidden"}')
                return
            if target.is_file():
                self._send_file(target, "application/json")
                return
            self._send(404, b'{"error":"not found"}')
            return

        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path.split("?", 1)[0].rstrip("/") != "/board.json":
            self._send(404, b'{"error":"not found"}')
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 50 * 1024 * 1024:
            self._send(413, b'{"error":"payload too large or empty"}')
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send(400, json.dumps({"error": f"bad json: {e}"}).encode())
            return
        if not isinstance(payload, dict) or "cards" not in payload:
            self._send(400, b'{"error":"missing cards"}')
            return

        global _cached_state
        with _write_lock:
            with _cached_lock:
                prev = _cached_state
            atomic_write(
                self.board_dir / "board.json",
                json.dumps(payload, indent=2).encode(),
            )
            regen_index(self.board_dir)
            with _cached_lock:
                _cached_state = payload

        events = diff_states(prev, payload)
        for name, data in events:
            broadcast(name, data)
        broadcast("rev-bumped", {
            "rev": payload.get("rev", 0),
            "savedBy": payload.get("savedBy", "?"),
            "savedAt": payload.get("savedAt", ""),
        })

        self._send(200, json.dumps({
            "ok": True,
            "rev": payload.get("rev", 0),
            "events": [n for n, _ in events],
        }).encode())


def _load_initial_cache(board_dir: Path) -> None:
    global _cached_state
    p = board_dir / "board.json"
    if p.is_file():
        try:
            _cached_state = json.loads(p.read_text())
        except Exception:
            _cached_state = None


def main():
    ap = argparse.ArgumentParser(description="board-steward local server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("BOARD_PORT", "7891")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--project", type=Path, default=None,
                    help="Project root (default: cwd; walks up looking for board/)")
    ap.add_argument("--board", type=Path, default=None,
                    help="Explicit board.json path (overrides --project)")
    ap.add_argument("--bootstrap", action="store_true",
                    help="If no board/ found, create one from the skill template")
    ap.add_argument("--profile", default="software",
                    choices=["software", "marketing", "research", "product", "operations"],
                    help="Tag taxonomy profile for bootstrap (default: software)")
    ap.add_argument("--install-hooks", action="store_true",
                    help="Wire UserPromptSubmit hook into Claude Code settings.json, then exit")
    ap.add_argument("--uninstall-hooks", action="store_true",
                    help="Remove the UserPromptSubmit hook, then exit")
    ap.add_argument("--hooks-status", action="store_true",
                    help="Report hook install state, then exit")
    args = ap.parse_args()

    if args.install_hooks or args.uninstall_hooks or args.hooks_status:
        import subprocess
        installer = Path(__file__).resolve().parent / "install_hooks.py"
        flag = (
            "--status" if args.hooks_status
            else "--uninstall" if args.uninstall_hooks
            else ""
        )
        cmd = [sys.executable, str(installer)] + ([flag] if flag else [])
        sys.exit(subprocess.run(cmd).returncode)

    if args.board:
        board_dir = args.board.resolve().parent
        if not args.board.exists():
            print(f"error: {args.board} does not exist", file=sys.stderr)
            sys.exit(2)
    else:
        start = (args.project or Path.cwd()).resolve()
        board_dir = find_board_dir(start)
        if board_dir is None:
            if args.bootstrap:
                board_dir = start / "board"
                bootstrap_board(board_dir, profile=args.profile)
                print(f"bootstrapped new board at {board_dir} (profile={args.profile})", file=sys.stderr)
                # Nudge first-time installers toward wiring the hook so the
                # board doesn't silently drift during long active-coding
                # sessions (root cause of card #84).
                _installer = Path(__file__).resolve().parent / "install_hooks.py"
                if _installer.exists():
                    import subprocess
                    rc = subprocess.run(
                        [sys.executable, str(_installer), "--status"],
                        capture_output=True, text=True,
                    ).returncode
                    if rc != 0:
                        print(
                            "\n💡 RECOMMENDED next step:\n"
                            f"   {sys.executable} {_installer}\n"
                            "   (wires a UserPromptSubmit hook so Claude updates the board automatically;\n"
                            "    one-time, idempotent, run `--uninstall-hooks` to reverse)\n",
                            file=sys.stderr,
                        )
            else:
                print(
                    f"error: no board/board.json found at or above {start}\n"
                    f"       pass --bootstrap to create a starter board",
                    file=sys.stderr,
                )
                sys.exit(2)

    BoardHandler.board_dir = board_dir
    _load_initial_cache(board_dir)
    httpd = ThreadingHTTPServer((args.host, args.port), BoardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"📋 board-steward v4 serving {board_dir} at {url} (SSE on /events)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        httpd.shutdown()


if __name__ == "__main__":
    main()
