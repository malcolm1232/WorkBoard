# Project Switcher Tab Row Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a top-left tab row to the board UI that switches between the user's multiple project boards, auto-starting a board's server if it's down.

**Architecture:** Two new additive, side-effect-light endpoints on `serve.py` — `GET /boards` (lists known projects from the port-assignments registry) and `POST /ensure-board` (health-checks a board's port and spawns its server detached if down) — plus a tab-row in `board.html` that fetches `/boards`, renders one pill per project, and on click calls `/ensure-board` then navigates same-tab. Each board stays its own isolated server/data/SSE; nothing here touches `board.json` or the CAS/recon write path.

**Tech Stack:** Python 3 stdlib `http.server` (serve.py), vanilla JS (board.html), `port_registry` module, `urllib`. Tests are standalone scripts in `dev/` matching the existing `dev/test_*.py` style (a `check()` helper + non-zero exit on failure), run with `python3 dev/test_841_*.py`.

## Global Constraints

- Endpoints are **additive only** — never mutate `board.json`, never touch the CAS/recon/SSE write path.
- `/ensure-board` must only spawn boards whose path is already a key in `~/.board-steward/port-assignments.json` — never an arbitrary path (process-spawn safety).
- Spawn pattern must match the existing one verbatim: `env -u CLAUDECODE nohup python3 <serve.py> --project <root> --port <port> > <log> 2>&1 </dev/null &` then `disown` (see `scripts/hook_session_start.sh:276` and `scripts/bootstrap_project.sh:64`).
- `board.html` is shared CODE served from `templates/board.html` (one source of truth for every board) — the tab row must work identically regardless of which board serves it.
- Project name labels reuse the existing `cleanProjectTitle(raw)` JS function (`templates/board.html` ~line 4625); do NOT duplicate that normalize logic in Python.
- Same-tab navigation; tab order by port ascending; auto-start health-poll timeout ≈ 5s.
- Respect the existing auth gate: new GET/POST handlers run after `self._gate()` exactly like sibling routes.
- Per user preference: do NOT git-commit/push unless the user explicitly asks. The commit steps below stage+commit locally only; skip the commit step if the user is iterating (still run all test steps).

---

### Task 1: `GET /boards` endpoint

Lists every known project for the tab row. Read-only.

**Files:**
- Modify: `scripts/serve.py` — add `port` class attr (~line 231), add route in `do_GET` (~line 450), add `_handle_boards` method, set `BoardHandler.port` in `main()` (~line 1020).
- Test: `dev/test_841_boards_endpoint.py`

**Interfaces:**
- Consumes: `port_registry.assignments()` → `{board_dir_path: port}`; `port_registry.read()` → `{board_dir_path: {port, pid, started_at}}`; `port_registry._pid_alive(pid)` → bool.
- Produces: `GET /boards` → JSON `{"boards": [{"path": str, "port": int, "title": str|None, "running": bool}, ...], "current_port": int}` ordered by `port` ascending. `BoardHandler.port` class attr (int) consumed by Task 2 and by this task's `current_port`.

- [ ] **Step 1: Write the failing test**

Create `dev/test_841_boards_endpoint.py`:

```python
#!/usr/bin/env python3
"""#841: GET /boards lists known projects (path, port, title, running) ordered
by port, plus current_port. Read-only — must not mutate board.json.

Run: python3 dev/test_841_boards_endpoint.py  → exit 0 = green, 1 = a fail.
"""
from __future__ import annotations
import json, sys, importlib
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
    assigns = {"/x/AAA/board": 7891, "/x/BBB/board": 7893, "/x/CCC/board": 7892}
    reg = {"/x/AAA/board": {"port": 7891, "pid": 111},
           "/x/CCC/board": {"port": 7892, "pid": 222}}  # BBB not running

    def fake_title(path):
        return {"/x/AAA/board": "WorkBoard",
                "/x/BBB/board": "QuantifyMe — Work Board",
                "/x/CCC/board": None}.get(str(path))

    cap = _Cap()
    h = make_handler(Path("/x/AAA/board"), 7891, cap)
    with mock.patch.object(pr, "assignments", lambda: assigns), \
         mock.patch.object(pr, "read", lambda: reg), \
         mock.patch.object(pr, "_pid_alive", lambda pid: True), \
         mock.patch.object(serve, "_board_title_for", fake_title):
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


if __name__ == "__main__":
    test()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 dev/test_841_boards_endpoint.py`
Expected: FAIL — `AttributeError: ... has no attribute '_handle_boards'` (and `serve` has no `_board_title_for`).

- [ ] **Step 3: Add the `port` class attr**

In `scripts/serve.py`, in `class BoardHandler` right after the `auth_token` attr (~line 232):

```python
    port: int | None = None  # set by main() — own port, for /boards current_port
```

- [ ] **Step 4: Add the title-reader helper**

In `scripts/serve.py` at module level (near the other module helpers, e.g. just above `class BoardHandler`):

```python
def _board_title_for(board_dir) -> str | None:
    """Best-effort read of a board's raw `title` for the /boards tab row.
    Returns None if the board.json is missing/unreadable. Normalization is done
    client-side (cleanProjectTitle) so the logic isn't duplicated here."""
    try:
        data = json.loads((Path(board_dir) / "board.json").read_text())
        t = data.get("title")
        return t if isinstance(t, str) and t.strip() else None
    except Exception:
        return None
```

- [ ] **Step 5: Add the `_handle_boards` method**

In `scripts/serve.py`, add to `class BoardHandler` (near `_handle_health`):

```python
    def _handle_boards(self):
        """GET /boards — #841 project switcher. Lists every known project from
        the port-assignments registry: path, port, raw title, and whether its
        server is currently alive. Read-only; never touches board.json."""
        import port_registry as _pr
        try:
            assigns = _pr.assignments() or {}
        except Exception:
            assigns = {}
        try:
            reg = _pr.read() or {}
        except Exception:
            reg = {}
        boards = []
        for path, port in assigns.items():
            try:
                if not Path(path).exists():
                    continue
            except Exception:
                continue
            entry = reg.get(path) or {}
            pid = entry.get("pid")
            running = False
            if pid:
                try:
                    running = _pr._pid_alive(int(pid))
                except Exception:
                    running = False
            boards.append({
                "path": path,
                "port": port,
                "title": _board_title_for(path),
                "running": running,
            })
        boards.sort(key=lambda b: b["port"])
        self._send(200, json.dumps({
            "boards": boards,
            "current_port": type(self).port,
        }).encode())
```

- [ ] **Step 6: Wire the route + set the class attr**

In `do_GET` (~line 449), add after the `/health` branch:

```python
        elif path == "/boards":
            self._handle_boards()
```

In `main()`, right after `BoardHandler.board_dir = board_dir` (~line 1020):

```python
    BoardHandler.port = args.port
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python3 dev/test_841_boards_endpoint.py`
Expected: `PASS` (exit 0).

- [ ] **Step 8: Commit** (skip if user is iterating — see Global Constraints)

```bash
git add scripts/serve.py dev/test_841_boards_endpoint.py
git commit -m "feat(#841): GET /boards lists projects for the switcher tab row"
```

---

### Task 2: `POST /ensure-board` endpoint

Health-checks a project's port; spawns its server detached if down; returns the URL.

**Files:**
- Modify: `scripts/serve.py` — add `do_POST` route (~line 641), add `_handle_ensure_board` + `_spawn_board` helper.
- Test: `dev/test_841_ensure_board.py`

**Interfaces:**
- Consumes: `port_registry.assignments()`; `BoardHandler._send`; the spawn one-liner.
- Produces: `POST /ensure-board?path=<board_dir>` → on success JSON `{"port": int, "url": "http://127.0.0.1:<port>"}` (200); unknown/absent path → `{"error": ...}` (400); spawn-timeout → `{"error": "could not start"}` (504).

- [ ] **Step 1: Write the failing test**

Create `dev/test_841_ensure_board.py`:

```python
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


def make_handler(path_qs, cap):
    h = serve.BoardHandler.__new__(serve.BoardHandler)
    h.path = f"/ensure-board?path={path_qs}"
    h._send = cap
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
         mock.patch.object(serve, "_port_healthy", lambda port: True), \
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
    def fake_health(port):       # down first, up after spawn
        health["calls"] += 1
        return health["calls"] > 1
    spawned = {"n": 0}
    def fake_spawn(board_dir, port): spawned["n"] += 1; return True
    with mock.patch.object(pr, "assignments", lambda: ASSIGNS), \
         mock.patch.object(serve, "_port_healthy", fake_health), \
         mock.patch.object(serve, "_spawn_board", fake_spawn):
        h._handle_ensure_board()
    check(cap.status == 200, "spawn path returns 200")
    check(spawned["n"] == 1, "spawned exactly once")
    check(json.loads(cap.body)["port"] == 7891, "returns the spawned port")


if __name__ == "__main__":
    test_unknown_path(); test_already_up(); test_spawn_when_down()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 dev/test_841_ensure_board.py`
Expected: FAIL — `AttributeError: ... '_handle_ensure_board'` (and `serve` lacks `_port_healthy`/`_spawn_board`).

- [ ] **Step 3: Add the health + spawn helpers**

In `scripts/serve.py` at module level (near `_board_title_for`):

```python
def _port_healthy(port: int, timeout: float = 0.4) -> bool:
    """True if a board server answers /health on this port."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _spawn_board(board_dir, port: int) -> bool:
    """Spawn a board server for board_dir on `port`, detached, then poll /health
    until up (~5s). Returns True once healthy. Uses the SAME launch pattern as
    hook_session_start.sh / bootstrap_project.sh so behavior is identical."""
    proj_root = str(Path(board_dir).resolve().parent)  # board_dir is <root>/board
    serve_py = str(Path(__file__).resolve())
    log = str(Path(proj_root) / ".board-server.log")
    env = dict(os.environ); env.pop("CLAUDECODE", None)
    try:
        with open(log, "ab") as lf:
            subprocess.Popen(
                [sys.executable, serve_py, "--project", proj_root,
                 "--port", str(port)],
                stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                start_new_session=True, env=env)
    except Exception:
        return False
    for _ in range(25):          # ~5s at 0.2s
        if _port_healthy(port):
            return True
        time.sleep(0.2)
    return False
```

- [ ] **Step 4: Add the `_handle_ensure_board` method**

In `scripts/serve.py`, add to `class BoardHandler`:

```python
    def _handle_ensure_board(self):
        """POST /ensure-board?path=<board_dir> — #841. Ensure that project's
        server is up (spawn if down) and return its url. Only spawns paths the
        registry already knows (no arbitrary process spawn)."""
        import port_registry as _pr
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        target = (qs.get("path") or [""])[0]
        try:
            assigns = _pr.assignments() or {}
        except Exception:
            assigns = {}
        if not target or target not in assigns:
            self._send(400, b'{"error":"unknown board path"}')
            return
        port = assigns[target]
        if not _port_healthy(port):
            if not _spawn_board(target, port):
                self._send(504, b'{"error":"could not start board"}')
                return
        self._send(200, json.dumps({
            "port": port,
            "url": f"http://127.0.0.1:{port}",
        }).encode())
```

- [ ] **Step 5: Wire the POST route**

In `do_POST` (~line 641), add as the first branch after `path` is computed:

```python
        if path == "/ensure-board":
            self._handle_ensure_board()
            return
```

(Place it before the existing `/progress` / board-write branches so it short-circuits cleanly. Verify the surrounding `do_POST` returns after each branch — match the existing style.)

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 dev/test_841_ensure_board.py`
Expected: `PASS` (exit 0).

- [ ] **Step 7: Commit** (skip if user is iterating)

```bash
git add scripts/serve.py dev/test_841_ensure_board.py
git commit -m "feat(#841): POST /ensure-board auto-starts a board server on demand"
```

---

### Task 3: Tab row UI in board.html

Render the pills, wire click → ensure-board → navigate.

**Files:**
- Modify: `templates/board.html` — add `<nav id="project-tabs">` as first child of `<header>` (~line 1674); add CSS for `.proj-tab`; add `loadProjectTabs()` + click handler in the script; call it on load.
- Test: `dev/test_841_ui_markup.py` (asserts the served HTML carries the hooks) + live check via `/e2e`.

**Interfaces:**
- Consumes: `GET /boards` (Task 1) → `{boards:[{path,port,title,running}], current_port}`; `POST /ensure-board` (Task 2); existing `cleanProjectTitle(raw)` JS fn.
- Produces: a `#project-tabs` nav with one `.proj-tab` button per board; no exports consumed downstream.

- [ ] **Step 1: Write the failing test**

Create `dev/test_841_ui_markup.py`:

```python
#!/usr/bin/env python3
"""#841: the served board.html must carry the project-switcher hooks — the
#project-tabs container, a loadProjectTabs() that fetches /boards, and a click
path that POSTs /ensure-board then navigates. Static markup/JS presence check.

Run: python3 dev/test_841_ui_markup.py  → exit 0 = green, 1 = a fail.
"""
from __future__ import annotations
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
html = (REPO / "templates" / "board.html").read_text()

_fails = 0
def check(cond, msg):
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond: _fails += 1

check('id="project-tabs"' in html, "has #project-tabs container")
check("loadProjectTabs" in html, "defines loadProjectTabs()")
check("/boards" in html, "fetches /boards")
check("/ensure-board" in html, "calls /ensure-board")
check("cleanProjectTitle(" in html, "labels via cleanProjectTitle")
check("window.location" in html and "ensure-board" in html, "navigates after ensure")

print("PASS" if _fails == 0 else f"FAIL ({_fails})")
sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 dev/test_841_ui_markup.py`
Expected: FAIL — `#project-tabs`, `loadProjectTabs`, `/ensure-board` not present.

- [ ] **Step 3: Add the markup**

In `templates/board.html`, make `#project-tabs` the first child of `<header>` (line ~1674), before `<span class="title-wrap">`:

```html
<header>
  <nav id="project-tabs" class="project-tabs" style="display:none"></nav>
  <span class="title-wrap">
```

- [ ] **Step 4: Add the CSS**

In the `<style>` block (near the other `.header-pill` rules), add:

```css
.project-tabs { display:flex; gap:6px; align-items:center; margin-right:10px; }
.project-tabs .proj-tab {
  font: inherit; font-size:12px; line-height:1; cursor:pointer;
  padding:5px 10px; border-radius:999px; border:1px solid var(--border, #2a2a2a);
  background:transparent; color:var(--muted, #9aa0a6); white-space:nowrap;
}
.project-tabs .proj-tab:hover { color:var(--fg, #e8eaed); }
.project-tabs .proj-tab.active {
  background:var(--accent, #3b82f6); color:#fff; border-color:transparent; cursor:default;
}
.project-tabs .proj-tab.starting { opacity:.6; cursor:progress; }
```

(If the board's palette uses different CSS variable names, match them; the fallbacks keep it readable regardless.)

- [ ] **Step 5: Add the JS (loader + click handler)**

In the `<script>` of `templates/board.html`, near `applyBoardTitle()` (~line 4636), add:

```javascript
// #841 — project switcher. Fetch the known boards and render a tab per project,
// top-left. Click → ensure that board's server is up → navigate same-tab.
async function loadProjectTabs() {
  const nav = document.getElementById('project-tabs');
  if (!nav) return;
  let data;
  try {
    const r = await fetch('/boards', { credentials: 'same-origin' });
    if (!r.ok) return;
    data = await r.json();
  } catch (_) { return; }
  const boards = (data && data.boards) || [];
  if (boards.length < 2) { nav.style.display = 'none'; return; }  // nothing to switch to
  nav.innerHTML = '';
  for (const b of boards) {
    const btn = document.createElement('button');
    btn.className = 'proj-tab' + (b.port === data.current_port ? ' active' : '');
    btn.textContent = cleanProjectTitle(b.title) || ('Board ' + b.port);
    btn.title = b.path + (b.running ? '' : ' (stopped — click to start)');
    if (b.port !== data.current_port) {
      btn.addEventListener('click', () => switchProject(btn, b));
    }
    nav.appendChild(btn);
  }
  nav.style.display = 'flex';
}

async function switchProject(btn, b) {
  if (btn.classList.contains('starting')) return;
  btn.classList.add('starting');
  const prev = btn.textContent;
  btn.textContent = 'starting…';
  try {
    const r = await fetch('/ensure-board?path=' + encodeURIComponent(b.path),
                          { method: 'POST', credentials: 'same-origin' });
    if (!r.ok) throw new Error('ensure failed');
    const j = await r.json();
    window.location = j.url;            // same-tab switch
  } catch (_) {
    btn.classList.remove('starting');
    btn.textContent = prev;
    if (typeof toast === 'function') toast("Couldn't start that board");
  }
}
```

> Note: if there is no existing `toast()` helper, drop the `toast(...)` line (the `typeof` guard already makes it a no-op when absent) — do not invent a new toast system for this.

- [ ] **Step 6: Call the loader on page load**

Find where the board first initializes (the existing DOMContentLoaded / init path that calls `applyBoardTitle()` / `wireBoardTitle()`), and add a call right after the board UI is wired:

```javascript
  loadProjectTabs();
```

(One call on load is enough — the tab list changes rarely; no need to poll. Match the existing init style; if init is inside a `DOMContentLoaded` handler, add it there.)

- [ ] **Step 7: Run the markup test to verify it passes**

Run: `python3 dev/test_841_ui_markup.py`
Expected: `PASS` (exit 0).

- [ ] **Step 8: Live smoke check (manual, no commit)**

The board server serves `templates/board.html` live from disk. With at least two boards assigned:

Run: `curl -s http://127.0.0.1:7891/boards | python3 -m json.tool`
Expected: a `boards` array (≥2) with `path/port/title/running` and a `current_port`.

Then reload `http://127.0.0.1:7891` in the browser and confirm: the tab row shows top-left, the current board's pill is filled, clicking another switches (and starts MarketingForWB/7894 if it was down). Report what you observe — do not claim success without this output.

- [ ] **Step 9: Commit** (skip if user is iterating)

```bash
git add templates/board.html dev/test_841_ui_markup.py
git commit -m "feat(#841): project switcher tab row in board.html"
```

---

### Task 4: End-to-end isolation check (optional, recommended)

Confirm the endpoints work against throwaway boards and the live board is untouched.

**Files:**
- Use: the existing `/e2e` harness (`skills/` e2e skill) — spins throwaway boards on isolated ports.

- [ ] **Step 1: Run the e2e harness**

Invoke the `/e2e` skill. Assert within it: `/boards` lists the throwaway boards with correct `running` flags; `/ensure-board` brings a deliberately-stopped throwaway board up (its `/health` flips to live); and the live board's `board.json` rev is unchanged throughout (pure additive endpoints).

- [ ] **Step 2: Record the result on the card**

Run: `/Users/malco/Desktop/WorkBoard/scripts/card.py fly 841 done --writeup "<endpoints + files + verification>" --board /Users/malco/Desktop/WorkBoard/board/board.json`

---

## Self-Review

**Spec coverage:**
- `GET /boards` (spec §1) → Task 1. ✓
- `POST /ensure-board` auto-start (spec §2) → Task 2. ✓
- Tab row UI, same-tab nav, order-by-port, <2 boards hides, starting state, error toast (spec §3 + defaults + edge cases) → Task 3. ✓
- Board file gone → omitted (spec edge case) → Task 1 `_handle_boards` `Path(path).exists()` skip. ✓
- Auto-start fails → toast + stay (spec edge case) → Task 3 `switchProject` catch. ✓
- Only one project → no tab row (spec edge case) → Task 3 `boards.length < 2`. ✓
- Click active tab no-op (spec edge case) → Task 3 (no click listener bound to active). ✓
- Unknown path → no spawn (spec edge case) → Task 2 `target not in assigns` → 400. ✓
- Testing via /e2e + untouched live board (spec §Testing) → Task 4. ✓
- Title normalized client-side, not in Python (spec §1 revision) → Task 1 returns raw `title`, Task 3 wraps in `cleanProjectTitle`. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases" — every code step shows full code. ✓

**Type consistency:** `_board_title_for(board_dir)→str|None`, `_port_healthy(port)→bool`, `_spawn_board(board_dir, port)→bool`, `_handle_boards`/`_handle_ensure_board` methods, `BoardHandler.port:int`, `/boards` shape `{boards:[{path,port,title,running}], current_port}`, `/ensure-board` shape `{port,url}` — names/shapes match across Tasks 1→3 and the tests. ✓
