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
import hmac
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_HTML = SKILL_DIR / "templates" / "board.html"
TEMPLATE_JSON = SKILL_DIR / "templates" / "board.json"
REGEN_SCRIPT = SKILL_DIR / "scripts" / "regen_index.py"

# Ensure scripts/ is importable (for `from port_registry import ...`) when
# launched via absolute path under launchd.
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import _boardio  # noqa: E402  (write-safety: flock + rolling backups)
import _render   # noqa: E402  (shared markdown/html renderers — #115 export)
import _metrics  # noqa: E402  (velocity metrics — #114)

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



# Bootstrap / discovery helpers live in serve_bootstrap.py (#307 file-split).
# Self-contained there; imported back here for the install + History Replay paths.
from serve_bootstrap import (  # noqa: E402
    bootstrap_board,
    _stream_discovered_cards,
    _stream_hourly_cards,
)


def atomic_write(path: Path, data: bytes) -> None:
    # Cross-process lock (3.5a): a `card.py` invoked from a shell that can't
    # reach this server writes the same file directly; the lock keeps the two
    # paths from interleaving. The in-process _write_lock alone doesn't cover that.
    with _boardio.board_lock(path):
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


def _wait_for_sse_client(timeout: float = 60.0, poll: float = 0.25) -> bool:
    """Block until at least one browser's EventSource is connected, or timeout.

    The install / History-Replay fly streams cards as SSE events with NO replay
    buffer. If it starts before the user's browser has finished loading and
    opened its EventSource, the cards animate to an empty audience (sseClients=0)
    and a later refresh shows only static end-state — the exact "I didn't see it
    fly" failure. Gating the live fly on a connected client guarantees it's
    actually watched. Returns False on timeout so headless / cron installs still
    build the board (they just don't wait forever for a browser that never opens).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _clients_lock:
            if _clients:
                return True
        time.sleep(poll)
    return False


def _gated_stream(fn, *fn_args) -> None:
    """Wait for a watching browser, then run the card-streaming fn. Keeps the
    install fly from animating to nobody (the sseClients=0 race)."""
    _wait_for_sse_client()
    fn(*fn_args)


class BoardHandler(BaseHTTPRequestHandler):
    board_dir: Path = None  # set by main()
    auth_token: str | None = None  # set by main() — #116 LAN-AUTH; None = open
    protocol_version = "HTTP/1.1"

    def _check_auth(self) -> tuple[bool, str | None]:
        """#116 BOARD-LAN-AUTH gate. Returns (ok, cookie_token).

        No token configured → always open (the localhost default; card.py and
        the local browser keep working untouched). When a token IS set, a
        request authenticates via any of:
          - Authorization: Bearer <token>   (card.py with BOARD_AUTH_TOKEN)
          - ?t=<token>                       (the URL you scan on your phone)
          - Cookie bs_auth=<token>           (set after the first ?t= hit)
        cookie_token is the token to Set-Cookie (non-None only when it arrived
        via ?t=, so the phone gets a cookie and later fetches/SSE just work).
        Constant-time compares so the gate isn't a timing oracle."""
        token = type(self).auth_token
        if not token:
            return True, None
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        qt = (qs.get("t") or [""])[0]
        if qt and hmac.compare_digest(qt, token):
            return True, qt
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
            return True, None
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "bs_auth" and hmac.compare_digest(v, token):
                    return True, None
        return False, None

    def _gate(self) -> bool:
        """Auth gate for a request. Sends 401 + returns False if unauthorized.
        Stashes a Set-Cookie header on self._cookie_extra when authed via ?t=."""
        ok, cookie_tok = self._check_auth()
        if not ok:
            self._send(401, b'{"error":"unauthorized"}',
                       extra={"WWW-Authenticate": "Bearer"})
            return False
        self._cookie_extra = (
            {"Set-Cookie": f"bs_auth={cookie_tok}; Path=/; SameSite=Strict; Max-Age=2592000"}
            if cookie_tok else {}
        )
        return True

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

    def _send_file(self, path: Path, ctype: str, extra: dict | None = None):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self._send(404, b'{"error":"not found"}')
            return
        self._send(200, data, ctype, extra=extra)

    def _send_tags_page(self):
        """Static-render the tag legend: canonical taxonomy (main + sub) +
        per-tag usage count + off-taxonomy 'wild' tags surfaced for cleanup.
        Plain HTML, no JS — read-only governance reference."""
        try:
            state = json.loads((self.board_dir / "board.json").read_text())
        except Exception:
            self._send(500, b'{"error":"board.json unreadable"}')
            return
        tt = state.get("tagTaxonomy") or {}
        main = tt.get("main") or []
        sub = tt.get("sub") or []
        canonical = {t.get("name"): t.get("color", "#888") for t in (main + sub) if t.get("name")}
        counts: dict[str, int] = {}
        for c in state.get("cards", []):
            for t in (c.get("tags") or []):
                counts[t] = counts.get(t, 0) + 1
        wild = sorted([t for t in counts if t not in canonical],
                      key=lambda x: -counts[x])

        def _row(name: str, color: str, count: int, kind: str) -> str:
            return (f'<tr><td><span class="sw" style="background:{color}"></span></td>'
                    f'<td><code>{name}</code></td>'
                    f'<td>{count}</td><td>{kind}</td></tr>')

        rows_main = "\n".join(
            _row(t["name"], t.get("color", "#888"), counts.get(t["name"], 0), "main")
            for t in main if t.get("name"))
        rows_sub = "\n".join(
            _row(t["name"], t.get("color", "#888"), counts.get(t["name"], 0), "sub")
            for t in sub if t.get("name"))
        rows_wild = "\n".join(
            _row(t, "#444", counts[t], "wild") for t in wild)
        wild_block = (
            f'<h2>Off-taxonomy tags ({len(wild)})</h2>'
            f'<p class="note">Added with <code>--force</code> or before the '
            f'taxonomy was tightened. Candidates for pruning.</p>'
            f'<table>{rows_wild}</table>') if wild else ""

        title = state.get("title", "WorkBoard")
        profile = tt.get("profile", "(unset)")
        html = (
            "<!doctype html><meta charset=utf-8>"
            f"<title>Tags — {title}</title>"
            "<style>"
            "body{font:14px/1.5 -apple-system,system-ui,sans-serif;"
            "background:#1a1a1a;color:#ddd;max-width:760px;margin:32px auto;padding:0 24px}"
            "h1{color:#eee;margin:0 0 4px}h2{color:#eee;margin-top:32px}"
            "p.note{color:#888;margin:4px 0 16px}"
            "table{border-collapse:collapse;width:100%;margin-bottom:8px}"
            "td{padding:6px 8px;border-bottom:1px solid #2a2a2a}"
            "code{color:#eee;background:#222;padding:1px 6px;border-radius:3px}"
            ".sw{display:inline-block;width:14px;height:14px;border-radius:3px;"
            "vertical-align:middle;border:1px solid #333}"
            "a{color:#7aa5d9}"
            "</style>"
            f"<h1>{title} — tag legend</h1>"
            f'<p class="note">Profile: <code>{profile}</code> · '
            f'{len(canonical)} canonical · {len(counts)} in use · '
            f'<a href="/">back to board</a></p>'
            f"<h2>Main ({len(main)})</h2><table>{rows_main}</table>"
            f"<h2>Sub ({len(sub)})</h2><table>{rows_sub}</table>"
            f"{wild_block}"
        )
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

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
        if not self._gate():
            return

        if path == "/events":
            self._handle_sse()
            return

        if path == "/" or path == "/board.html":
            html_path = self.board_dir / "board.html"
            if not html_path.exists():
                html_path = TEMPLATE_HTML
            # On a ?t= hit, hand back a cookie so subsequent board.json / SSE /
            # POST requests from this browser authenticate automatically — no
            # board.html changes needed.
            self._send_file(html_path, "text/html; charset=utf-8",
                            extra=getattr(self, "_cookie_extra", None))
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

        if path == "/metrics":
            # #114 BOARD-METRICS — velocity JSON. ?since=Nd window (default 7).
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            raw = (qs.get("since") or ["7"])[0].strip().rstrip("d")
            since_days = int(raw) if raw.isdigit() and int(raw) > 0 else 7
            try:
                state = json.loads((self.board_dir / "board.json").read_text())
            except Exception:
                self._send(500, b'{"error":"board.json unreadable"}')
                return
            self._send(200, json.dumps(_metrics.compute(state, since_days),
                                       ensure_ascii=False).encode("utf-8"))
            return

        if path in ("/export.md", "/export.html"):
            # #115 BOARD-EXPORT — static shareable snapshot. ?since=Nd narrows
            # the recently-shipped section to a sprint window (e.g. ?since=7d).
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            since_days = None
            raw = (qs.get("since") or [""])[0].strip().rstrip("d")
            if raw.isdigit():
                since_days = int(raw)
            try:
                state = json.loads((self.board_dir / "board.json").read_text())
            except Exception:
                self._send(500, b'{"error":"board.json unreadable"}')
                return
            if path == "/export.html":
                body = _render.to_html(state, recent=20, since_days=since_days)
                ctype = "text/html; charset=utf-8"
            else:
                body = _render.to_markdown(state, recent=20, since_days=since_days)
                ctype = "text/markdown; charset=utf-8"
            self._send(200, body.encode("utf-8"), ctype)
            return

        if path == "/flash":
            # #102 BOARD-AUTO-LINK — broadcast a transient flash to the board.
            # No state mutation; just a one-shot SSE pulse. Query params:
            #   ?card=<num|id> — required, the card to flash
            #   ?file=<path>   — optional, file path that triggered the flash
            #                    (shown in the toast)
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            ref = (qs.get("card") or [""])[0]
            fpath = (qs.get("file") or [""])[0]
            if not ref:
                self._send(400, b'{"error":"missing card param"}')
                return
            try:
                state = json.loads((self.board_dir / "board.json").read_text())
            except Exception:
                self._send(500, b'{"error":"board.json unreadable"}')
                return
            target = None
            for c in state.get("cards", []):
                if str(c.get("num")) == ref or c.get("id") == ref or c.get("code") == ref:
                    target = c
                    break
            if target is None:
                self._send(404, json.dumps({"error": f"card not found: {ref}"}).encode())
                return
            broadcast("card-flash", {
                "id": target["id"],
                "num": target.get("num"),
                "title": target.get("title", ""),
                "file": fpath,
            })
            self._send(200, json.dumps({"ok": True, "flashed": target.get("num")}).encode())
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

        if path == "/tags":
            self._send_tags_page()
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
        if not self._gate():
            return
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
        body_out = json.dumps(payload, indent=2).encode()
        with _write_lock:
            with _cached_lock:
                prev = _cached_state
            atomic_write(self.board_dir / "board.json", body_out)
            _boardio.write_backup(self.board_dir / "board.json", body_out)  # 3.5b
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


# Canonical default columns. Used to add missing cols on load so existing
# boards from older templates gain the new defaults without manual edits.
_DEFAULT_COLS = [
    {"id": "task",       "name": "📥 Task",      "kind": "todo",   "stackUnder": None},
    {"id": "backlog",    "name": "Backlog",      "kind": "todo",   "stackUnder": None},
    {"id": "inprogress", "name": "In Progress",  "kind": "active", "stackUnder": None},
    {"id": "done",       "name": "Done",         "kind": "done",   "stackUnder": None},
    {"id": "notes",      "name": "📝 Notes",     "kind": "intake", "stackUnder": None},
    {"id": "mandatory",  "name": "📌 MANDATORY", "kind": "todo",   "stackUnder": None},
]


def _migrate_default_cols(state: dict, board_path: Path) -> bool:
    """Append any default cols missing from the board. Idempotent.
    Match by id OR case-insensitive name so a user's hand-named "notes"
    column doesn't get duplicated. Returns True if state changed."""
    cols = state.get("columns") or []
    existing_ids = {c.get("id") for c in cols}
    existing_names = {(c.get("name") or "").lower().strip("📥📝📌🚨💡 ") for c in cols}
    added = []
    for d in _DEFAULT_COLS:
        nm = d["name"].lower().strip("📥📝📌🚨💡 ")
        if d["id"] in existing_ids or nm in existing_names:
            continue
        added.append(d)
    if not added:
        return False
    state.setdefault("columns", []).extend(added)
    return True


def _load_initial_cache(board_dir: Path) -> None:
    global _cached_state
    p = board_dir / "board.json"
    if p.is_file():
        try:
            _cached_state = json.loads(p.read_text())
        except Exception:
            _cached_state = None
            return
        if _migrate_default_cols(_cached_state, p):
            try:
                _cached_state["rev"] = (_cached_state.get("rev") or 0) + 1
                atomic_write(p, json.dumps(_cached_state, indent=2,
                                          ensure_ascii=False).encode("utf-8"))
            except Exception:
                pass


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
    ap.add_argument("--share", action="store_true",
                    help="Opt out of auto-gitignoring board/. Use when you intentionally want to commit a shared board.")
    ap.add_argument("--auth-token", default=os.environ.get("BOARD_AUTH_TOKEN"),
                    help="#116 — require this bearer token on every request "
                         "(Authorization: Bearer / ?t= / cookie). Pair with "
                         "--host 0.0.0.0 to glance at the board on your phone. "
                         "Defaults to $BOARD_AUTH_TOKEN.")
    ap.add_argument("--profile", default="software",
                    choices=["software", "marketing", "research", "product", "operations"],
                    help="Tag taxonomy profile for bootstrap (default: software)")
    ap.add_argument("--title", default=None,
                    help="Override auto-detected project name in the board title")
    ap.add_argument("--no-discover", action="store_true",
                    help="On bootstrap, do NOT mine prior Claude sessions into cards (default: do)")
    ap.add_argument("--discover-days", type=int, default=7,
                    help="Discover sessions touched in the last N days (default 7)")
    ap.add_argument("--discover-max", type=int, default=20,
                    help="Cap how many tasks become cards on bootstrap (default 20, discover mode only)")
    ap.add_argument("--bootstrap-mode", choices=["inline", "haiku", "discover"], default="inline",
                    help="How bootstrap fills the board: 'inline' (default) = FREE — stage "
                         "extraction_pending.json for the main Claude session to emit (no "
                         "Haiku cost, highest quality); 'haiku' = claude -p per bucket "
                         "(costs usage, runs in background); 'discover' = cheap discover2 plop")
    ap.add_argument("--bucket-min", type=int, default=30,
                    help="hourly bootstrap: minutes per bucket (default 30)")
    ap.add_argument("--chunk-size", type=int, default=2,
                    help="hourly bootstrap: buckets per Haiku call (default 2)")
    ap.add_argument("--harvest-project", type=Path, default=None,
                    help="hourly bootstrap: mine history from THIS project while "
                         "the board lives in --project (isolated sim/--demo; "
                         "default: same as --project)")
    ap.add_argument("--legacy-discover", action="store_true",
                    help="Use the older discover.py (session-shaped) instead of discover2.py (task-shaped); forces discover mode")
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
                bootstrap_board(board_dir, profile=args.profile,
                                title_override=args.title,
                                share=args.share)
                print(f"bootstrapped new board at {board_dir} (profile={args.profile})", file=sys.stderr)
                # Stream cards from prior Claude sessions in a background
                # thread so the user watches their history fill in. Opt out
                # with --no-discover for a genuine empty start.
                #
                # Two fill modes:
                #   hourly  (default) — HIGH-COMPUTE: hourly_extractor multi-source
                #                       harvest + Haiku-per-bucket + flying quality
                #                       cards (the chosen startup behaviour, #268).
                #   discover          — cheap discover2 'plop' (no API key needed).
                if not args.no_discover:
                    if args.bootstrap_mode in ("inline", "haiku") and not args.legacy_discover:
                        _tgt = _stream_hourly_cards
                        _targs = (start, board_dir, args.port,
                                  args.discover_days, args.bucket_min,
                                  args.chunk_size,
                                  args.harvest_project.resolve()
                                  if args.harvest_project else None,
                                  args.bootstrap_mode)
                        # inline only STAGES (main Claude emits later) — nothing
                        # to watch, so don't make staging wait on a browser.
                        _flies = (args.bootstrap_mode == "haiku")
                    else:
                        _tgt = _stream_discovered_cards
                        _targs = (start, board_dir, args.port,
                                  args.discover_days, args.discover_max,
                                  0.25, args.legacy_discover,
                                  args.harvest_project.resolve()
                                  if args.harvest_project else None)
                        _flies = True  # discover plops cards live → gate on a viewer
                    # Gate the LIVE fly on a connected browser so cards never
                    # stream to an empty audience (sseClients=0). Staging-only
                    # (inline) runs immediately.
                    if _flies:
                        _thread_target = (lambda t=_tgt, a=_targs: _gated_stream(t, *a))
                    else:
                        _thread_target = (lambda t=_tgt, a=_targs: t(*a))
                    threading.Thread(target=_thread_target, daemon=True).start()
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
    BoardHandler.auth_token = args.auth_token or None
    _load_initial_cache(board_dir)
    httpd = ThreadingHTTPServer((args.host, args.port), BoardHandler)
    url = f"http://{args.host}:{args.port}"
    # #107 — register port BEFORE serve_forever so card.py / hooks resolve us
    # O(1) instead of probing 7891-7900. Best-effort; if the registry write
    # fails the probe path still works.
    try:
        from port_registry import write as _registry_write, remove as _registry_remove
        _registry_write(board_dir, args.port, os.getpid())
    except Exception as e:  # pragma: no cover — fail open
        _registry_remove = None
        print(f"warn: port-registry write failed: {e}", file=sys.stderr)
    print(f"📋 board-steward v4 serving {board_dir} at {url} (SSE on /events)", flush=True)
    if BoardHandler.auth_token:
        # #116 — print the scan-me URL with the token baked in. Detect the
        # primary LAN IP without sending a packet (UDP connect just picks the
        # route's source address).
        lan_ip = args.host
        if args.host in ("0.0.0.0", "::"):
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("10.255.255.255", 1))
                lan_ip = s.getsockname()[0]
                s.close()
            except Exception:
                lan_ip = "<lan-ip>"
        print(f"🔒 auth ON — open on another device:\n"
              f"   http://{lan_ip}:{args.port}/?t={BoardHandler.auth_token}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        httpd.shutdown()
    finally:
        if _registry_remove is not None:
            try:
                _registry_remove(board_dir)
            except Exception:
                pass


if __name__ == "__main__":
    main()
