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
import re
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

# Ensure scripts/ is importable (for `from port_registry import ...`) when
# launched via absolute path under launchd.
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

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


def _detect_project_name(project_root: Path) -> str:
    """Pick the friendliest project name we can find. Order of preference:
    package.json `name`, pyproject.toml `project.name`, CONTEXT.md first H1,
    cwd basename. Falls back to 'WorkBoard' if everything is empty."""
    pkg = project_root / "package.json"
    if pkg.is_file():
        try:
            name = json.loads(pkg.read_text()).get("name", "").strip()
            if name:
                return name
        except Exception:
            pass
    pyproj = project_root / "pyproject.toml"
    if pyproj.is_file():
        try:
            m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']',
                          pyproj.read_text(), re.MULTILINE)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    ctx = project_root / "CONTEXT.md"
    if ctx.is_file():
        try:
            for line in ctx.read_text().splitlines():
                m = re.match(r'^#\s+(.+?)\s*$', line)
                if m:
                    return m.group(1).strip()
        except Exception:
            pass
    return project_root.name or "WorkBoard"


_GITIGNORE_BLOCK = "# Added by board-steward — local board data may contain secrets"
_GITIGNORE_ENTRIES = (
    "board/",
    "board/board.json",
    "board/index.json",
    "board/extraction_snapshot.json",
    "board/recon_pending.json",
)


def _ensure_gitignore(project_root: Path) -> str | None:
    """Idempotently append board artifacts to project's .gitignore if the
    project is a git repo. Returns a one-line status for the caller to print,
    or None if nothing to do (not a repo). Safe to call repeatedly."""
    if not (project_root / ".git").exists():
        return None
    gi = project_root / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    missing = [e for e in _GITIGNORE_ENTRIES if e not in existing_lines]
    if not missing:
        return f"{gi} already covers board artifacts"
    block = "\n" + _GITIGNORE_BLOCK + "\n" + "\n".join(missing) + "\n"
    if existing and not existing.endswith("\n"):
        block = "\n" + block
    with gi.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return f"{gi} updated (+{len(missing)} entries)"


def bootstrap_board(board_dir: Path, profile: str = "software",
                    title_override: str | None = None,
                    share: bool = False) -> None:
    """First-run init: create board/ with starter board.json + board.html.
    Title resolves via `_detect_project_name` (package.json / pyproject.toml /
    CONTEXT.md / basename) unless `title_override` is given. Tag taxonomy
    seeded from the chosen industry profile. When `share` is False (default)
    and the project is a git repo, board artifacts are added to .gitignore so
    user-private content (titles, origins, secrets) doesn't get committed."""
    board_dir.mkdir(parents=True, exist_ok=True)
    target_json = board_dir / "board.json"
    if not target_json.exists() and TEMPLATE_JSON.exists():
        data = json.loads(TEMPLATE_JSON.read_text())
        project_name = title_override or _detect_project_name(board_dir.parent)
        data["title"] = f"WorkBoard — {project_name}"
        data["tagTaxonomy"] = _load_tag_profile(profile)
        target_json.write_text(json.dumps(data, indent=2))
    target_html = board_dir / "board.html"
    if not target_html.exists() and TEMPLATE_HTML.exists():
        target_html.write_text(TEMPLATE_HTML.read_text())
    if not share:
        status = _ensure_gitignore(board_dir.parent)
        if status:
            print(f"gitignore: {status}", file=sys.stderr)


def _task_to_card_args(task: dict) -> list[str] | None:
    """Map a discover2.py task record to `card.py add` CLI args. Uses both
    files_touched_all (work intensity) and files_touched_in_proj (relevance),
    so sessions that edit sibling-repo notes still get credit (Bug 1 fix)."""
    title_seed = (task.get("user_prompt") or "").strip()
    files_all = task.get("files_touched_all") or []
    files_proj = task.get("files_touched_in_proj") or []
    ship = task.get("ship_hits_clean") or []
    defer = task.get("defer_hits") or []
    bugs = task.get("bug_hits") or []
    commits = task.get("git_commits") or []

    if not title_seed and not files_all and not commits:
        return None
    if len(title_seed) < 15 and not files_all and not ship and not commits:
        return None

    title = (title_seed[:80]
             or (f"Worked on {Path(files_all[0]).name}" if files_all else "Task"))
    if "\n" in title:
        title = title.split("\n", 1)[0]
    title = title.rstrip()

    # Column heuristic — ship needs BOTH a clean ship hit AND file activity OR
    # a git commit landed inside the task window.
    try:
        ended_dt = datetime.fromisoformat(
            (task.get("ts_end") or "").replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - ended_dt).days
    except Exception:
        age_days = 999

    real_ship = (bool(ship) and bool(files_all)) or bool(commits)
    urgency = task.get("urgency_hits") or []

    if real_ship:
        column = "done"
    elif urgency and not real_ship:
        column = "mandatory"               # urgency-language → mandatory
    elif defer:
        column = "backlog"
    elif age_days <= 2 and files_all:
        column = "inprogress"
    elif age_days <= 3 and not files_all:
        column = "task"
    elif bugs and not real_ship:
        column = "backlog"
    elif age_days > 7 and files_all:
        column = "done"
    elif age_days > 7:
        column = "backlog"
    else:
        column = "backlog"

    tags = []
    if bugs: tags.append("bug")
    if defer: tags.append("deferred")
    if real_ship: tags.append("shipped")
    if urgency: tags.append("mandatory")

    ended = (task.get("ts_end") or "")[:10]
    sid = (task.get("sessionId") or "")[:8]
    origin = (f"Discovered by discover2 (bucket {task.get('bucket_id')}, "
              f"session {sid}, {ended}). User said: \"{title_seed[:300]}\"")

    notes_parts: list[str] = []
    dur = task.get("duration_min", 0)
    n_user = task.get("n_user_total", 0)
    src = ", ".join(task.get("source_set") or [])
    notes_parts.append(f"Task: {n_user} user turn(s) over {dur}min. Sources: {src}.")
    if files_proj:
        notes_parts.append("In-proj files: " + ", ".join(files_proj[:8])
                           + ("..." if len(files_proj) > 8 else ""))
    elif files_all:
        notes_parts.append("Files touched: " + ", ".join(
            Path(f).name for f in files_all[:8]))
    if commits:
        notes_parts.append("Commits: " + " / ".join(
            f"{c.get('shaShort') or c.get('sha','')[:7]} {c.get('subj','')[:60]}"
            for c in commits[:3]))
    if ship:
        notes_parts.append("Ship signals: " + " / ".join(s[:120] for s in ship[:3]))
    if defer:
        notes_parts.append("Defer signals: " + " / ".join(s[:120] for s in defer[:3]))
    if bugs:
        notes_parts.append("Bug signals: " + " / ".join(s[:120] for s in bugs[:3]))
    notes = "\n".join(notes_parts)

    args = ["--column", column, "--priority", "mid", "--title", title,
            "--origin", origin, "--notes", notes, "--tag", "discovered"]
    for t in tags:
        args += ["--tag", t]
    return args


def _session_to_card_args(session: dict) -> list[str] | None:
    """Map a discover.py session summary to `card.py add` CLI args. Returns
    None if the session is too thin to be worth carding."""
    first = (session.get("firstUserPrompt") or "").strip()
    files = session.get("filesEdited") or []
    ship = session.get("shipHints") or []
    defer = session.get("deferHints") or []
    bugs = session.get("bugHints") or []

    # Confidence filter — skip sessions with no real signal.
    if not first and not files:
        return None
    if len(first) < 15 and not files and not ship:
        return None

    title = (first[:80] or (f"Worked on {Path(files[0]).name}" if files else "Session")).rstrip()
    if "\n" in title:
        title = title.split("\n", 1)[0]

    # Column heuristic — spread cards across Task / In Progress / Backlog / Done
    # by recency + signal density. The board should reflect last-week-of-work
    # mix, not be a graveyard of "Done".
    try:
        ended_dt = datetime.fromisoformat(
            (session.get("endedAt") or "").replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - ended_dt).days
    except Exception:
        age_days = 999

    # SHIP_RE matches "done|fixed|works|live" — Claude says those casually in
    # nearly every reply, so a ship-hint alone is noisy. Trust it only when
    # there's actual file activity to back it up.
    real_ship = bool(ship) and bool(files)

    if real_ship:
        column = "done"                          # actually shipped work
    elif defer:
        column = "backlog"                       # explicit defer wins
    elif age_days <= 2 and files:
        column = "inprogress"                    # recent + editing → in flight
    elif age_days <= 3 and not files:
        column = "task"                          # recent talk, no work yet
    elif bugs and not real_ship:
        column = "backlog"                       # open bug
    elif age_days > 7 and files:
        column = "done"                          # old work with edits → done
    elif age_days > 7:
        column = "backlog"                       # old chatter, no work
    else:
        column = "backlog"                       # default: mentioned, unactioned

    tags = []
    if bugs: tags.append("bug")
    if defer: tags.append("deferred")
    if real_ship: tags.append("shipped")

    ended = (session.get("endedAt") or "")[:10]
    sid = (session.get("sessionId") or "")[:8]
    origin = f"Discovered from session {sid} ({ended}). User said: \"{first[:300]}\""

    notes_parts = []
    dur = session.get("durationMin", 0)
    turns = session.get("turns", {})
    notes_parts.append(f"Session: {turns.get('user',0)} user / {turns.get('assistant',0)} asst turns over {dur}min.")
    if files:
        notes_parts.append("Files: " + ", ".join(files[:10]) + ("..." if len(files) > 10 else ""))
    if ship:
        notes_parts.append("Ship signals: " + " / ".join(s[:120] for s in ship[:3]))
    if defer:
        notes_parts.append("Defer signals: " + " / ".join(s[:120] for s in defer[:3]))
    if bugs:
        notes_parts.append("Bug signals: " + " / ".join(s[:120] for s in bugs[:3]))
    notes = "\n".join(notes_parts)

    args = ["--column", column, "--priority", "mid", "--title", title,
            "--origin", origin, "--notes", notes, "--tag", "discovered"]
    for t in tags:
        args += ["--tag", t]
    return args


def _stream_discovered_cards(project_root: Path, board_dir: Path,
                              port: int, days: int, max_items: int,
                              delay_s: float = 0.25,
                              legacy: bool = False) -> None:
    """Background-thread worker: run discover2.py (or discover.py if --legacy),
    then issue `card.py add` for each task at `delay_s` pacing. Cards land via
    the live HTTP server, which fires SSE events — the browser animates them in."""
    script_dir = Path(__file__).resolve().parent
    discover_py = script_dir / ("discover.py" if legacy else "discover2.py")
    card_py = script_dir / "card.py"
    if not discover_py.exists() or not card_py.exists():
        return

    for _ in range(20):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.3).read()
            break
        except Exception:
            time.sleep(0.2)

    cmd = [sys.executable, str(discover_py),
           "--project", str(project_root),
           "--days", str(days)]
    cmd += ["--max-sessions" if legacy else "--max-tasks", str(max_items)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return
        data = json.loads(out.stdout)
    except Exception:
        return

    if legacy:
        items = data.get("sessions", [])
        mapper = _session_to_card_args
    else:
        items = data.get("tasks", [])
        mapper = _task_to_card_args
    if not items:
        return

    n_added = 0
    for item in items[:max_items]:
        args = mapper(item)
        if not args:
            continue
        try:
            subprocess.run(
                [sys.executable, str(card_py), "--board",
                 str(board_dir / "board.json"), "add"] + args,
                capture_output=True, text=True, timeout=10,
            )
            n_added += 1
        except Exception:
            pass
        time.sleep(delay_s)

    print(f"discover{'(legacy)' if legacy else '2'}: streamed {n_added} card(s) "
          f"from {len(items)} {'session' if legacy else 'task'}(s)",
          file=sys.stderr)


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
                    help="Cap how many tasks become cards on bootstrap (default 20)")
    ap.add_argument("--legacy-discover", action="store_true",
                    help="Use the older discover.py (session-shaped) instead of discover2.py (task-shaped)")
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
                if not args.no_discover:
                    threading.Thread(
                        target=_stream_discovered_cards,
                        args=(start, board_dir, args.port,
                              args.discover_days, args.discover_max,
                              0.25, args.legacy_discover),
                        daemon=True,
                    ).start()
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
