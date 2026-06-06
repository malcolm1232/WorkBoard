#!/usr/bin/env python3
"""board-steward state/IO toolkit — extracted from card.py (#307 file-split, 3-way).

Board locating, atomic state I/O (server-POST or direct file write w/ flock +
rolling backup), card lookup, slug/tag/urgency helpers, and subtask-tree
helpers. Imported by card.py (CLI entry) and card_commands.py. No CLI here.
"""
from __future__ import annotations

import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REGEN_SCRIPT = SKILL_DIR / "scripts" / "regen_index.py"

# Ensure scripts/ is importable for the sibling _boardio helper even when this
# module is imported rather than run (its dir is sys.path[0] only when run).
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
import _boardio  # noqa: E402  (write-safety: flock + rolling backups)

# Set True by card.py::main() while it holds the cross-process file lock for the
# no-server direct-write path. atomic_save reads it to skip POST + re-lock.
_HOLDING_LOCK = False


# ===== board locating =====

def find_board(explicit: Path | None) -> Path:
    if explicit:
        p = explicit.resolve()
        if not p.is_file():
            sys.exit(f"error: {p} not found")
        return p
    cur = Path.cwd().resolve()
    for _ in range(8):
        c = cur / "board" / "board.json"
        if c.is_file():
            return c
        if cur.parent == cur:
            break
        cur = cur.parent
    sys.exit("error: no board/board.json found at or above cwd (pass --board)")


# ===== state I/O =====

def load(p: Path) -> dict:
    with open(p) as f:
        return json.load(f)


def _auth_headers() -> dict:
    """#116 — if the server requires a bearer token, card.py reads it from
    $BOARD_AUTH_TOKEN so local writes still funnel through the server (and
    animate). Empty dict when no token is set (the common localhost case)."""
    tok = os.environ.get("BOARD_AUTH_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _verify_port_owns_board(port: int, want_dir: str) -> bool:
    """Health-ping a single port and confirm its `board` field matches `want_dir`.
    Returns False on any failure (timeout, wrong board, dead port)."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health", headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=0.4) as r:
            if r.status != 200:
                return False
            info = json.loads(r.read())
            got = info.get("board")
            return bool(got and Path(got).resolve() == Path(want_dir).resolve())
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError,
            json.JSONDecodeError):
        return False


def _resolve_server_url(board_path: Path) -> str | None:
    """Find the running board server that owns this board.json path.

    Priority order:
      1. $BOARD_SERVER env var (explicit override).
      2. Port registry (~/.config/board-steward/port-registry.json) — O(1)
         lookup, verified via /health to handle stale entries (#107).
      3. Probe [7891, 7900] /health and match by `board` field — safety net
         when registry missing or out of sync.

    Returns the URL on a match, None if no server claims this board (caller
    falls back to direct file write — never POSTs to a wrong server)."""
    env_url = os.environ.get("BOARD_SERVER")
    if env_url:
        return env_url
    want = str(board_path.parent.resolve())

    # Registry-first (#107)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import port_registry
        cached_port = port_registry.lookup(want)
        if cached_port and _verify_port_owns_board(cached_port, want):
            return f"http://127.0.0.1:{cached_port}"
    except Exception:
        pass  # any registry failure → fall through to probe

    # Probe fallback (back-compat, also handles unregistered ad-hoc servers)
    for port in range(7891, 7901):
        if _verify_port_owns_board(port, want):
            return f"http://127.0.0.1:{port}"
    return None


def _try_post_to_server(d: dict, board_path: Path) -> bool:
    """POST the state to the running board server that owns this board.

    The server diffs vs prev cached state, broadcasts SSE events, writes the
    file atomically, and regenerates index.json. Returns True on success.
    If no server owns this board, returns False so caller falls back to
    direct file write — NEVER posts to a server with a different board.
    """
    if os.environ.get("BOARD_NO_SERVER") == "1":
        return False
    url = _resolve_server_url(board_path)
    if not url:
        return False
    try:
        body = json.dumps(d, indent=2, ensure_ascii=False).encode()
        req = urllib.request.Request(
            url.rstrip("/") + "/board.json",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", **_auth_headers()},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False


def _write_direct(p: Path, data: bytes) -> None:
    """Atomic file write + rolling backup (3.5b). Caller decides locking."""
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".board.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise
    _boardio.write_backup(p, data)


def atomic_save(p: Path, d: dict, regen: bool = True) -> int:
    """Bump rev, set savedAt/savedBy=claude.

    Preferred path: POST to the running board server so SSE clients animate
    the change in real-time. Fallback: write the file directly + regen index.
    Returns new rev.

    Concurrency (3.5a): when no server owns the board, main() holds the
    cross-process file lock across the whole load→mutate→save so two direct
    writers can't both read the same rev and clobber each other (lost update).
    While that lock is held (_HOLDING_LOCK) we MUST NOT POST — a server's own
    write also grabs the file lock, so POSTing under it would deadlock — and we
    must NOT re-lock (flock on a second fd in the same process self-deadlocks).
    """
    d["rev"] = d.get("rev", 0) + 1
    d["savedAt"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    d["savedBy"] = "claude"

    data = json.dumps(d, indent=2, ensure_ascii=False).encode()
    if _HOLDING_LOCK:
        # No-server path: caller (main) already holds the lock and chose direct.
        _write_direct(p, data)
    else:
        if _try_post_to_server(d, p):
            return d["rev"]
        # Server vanished mid-command → self-lock this one write.
        with _boardio.board_lock(p):
            _write_direct(p, data)
    if regen and REGEN_SCRIPT.exists():
        subprocess.run(
            [sys.executable, str(REGEN_SCRIPT), str(p)],
            capture_output=True, timeout=10, check=False,
        )
    return d["rev"]


# ===== card lookup =====

def find_card(d: dict, ref: str) -> dict:
    """Look up a card by num (int or '#N') or id."""
    if ref.startswith("#"):
        ref = ref[1:]
    try:
        n = int(ref)
        for c in d["cards"]:
            if c.get("num") == n:
                return c
    except ValueError:
        pass
    for c in d["cards"]:
        if c.get("id") == ref or c.get("code") == ref:
            return c
    sys.exit(f"error: no card matches '{ref}'")


def now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def maybe_stdin(literal: str | None, stdin_flag: bool) -> str | None:
    """If --foo-stdin set, read from stdin; else return literal (may be None)."""
    if stdin_flag:
        return sys.stdin.read().rstrip("\n")
    return literal


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:32]


def _taxonomy_names(d: dict) -> list[str]:
    """All canonical tag names from board.json tagTaxonomy.main + sub."""
    tt = d.get("tagTaxonomy") or {}
    names = [t.get("name", "") for t in (tt.get("main") or []) if t.get("name")]
    names += [t.get("name", "") for t in (tt.get("sub") or []) if t.get("name")]
    return names


# Structural marker tags — NOT domain taxonomy. They mark a card's *kind* for
# tooling (recon, the phase-card guard), so they bypass taxonomy validation and
# never consume a profile slot. `phase` (#107) marks a roadmap phase card the
# fly-guard refuses to send to inprogress; `discovered` is the recon provenance
# marker (also appended directly elsewhere).
_STRUCTURAL_TAGS = {"phase", "discovered"}


def _check_tags(tags: list[str], d: dict, force: bool) -> list[str]:
    """Filter tags against board.json taxonomy. Unknown tags are blocked unless
    --force, with a close-match suggestion printed to stderr. Returns the
    accepted tag list. Empty taxonomy = pass-through (back-compat). Structural
    marker tags (_STRUCTURAL_TAGS) always pass."""
    taxonomy = _taxonomy_names(d)
    if not taxonomy:
        return tags
    accepted = []
    for t in tags or []:
        if t in taxonomy or t in _STRUCTURAL_TAGS:
            accepted.append(t)
            continue
        if force:
            accepted.append(t)
            continue
        # blocked: surface a close match if any
        match = difflib.get_close_matches(t, taxonomy, n=1, cutoff=0.6)
        hint = f" did you mean '{match[0]}'?" if match else ""
        print(f"warning: tag '{t}' not in taxonomy.{hint} "
              f"Pass --force to add as-is, or use a canonical tag "
              f"(see board.json tagTaxonomy or http://<server>/tags).",
              file=sys.stderr)
    return accepted


# ===== auto-urgent detection (#85) =====
# Card-level urgency detection. When `card.py add` finds an urgency signal in
# the title or origin, the card lands in the 🚨 SUPER URGENT column (auto-created
# if missing) with priority bumped to critical. Telemetry event logged so noise
# can be reviewed via report.py. Opt out per-call via --no-auto-urgent.

# Strong markers always trigger (case-insensitive, word-boundary).
_AUTO_URGENT_STRONG = re.compile(
    r"\b(super\s+urgent|asap|p0|emergency|blocker|production\s+down|prod\s+down)\b",
    re.IGNORECASE,
)
# Weak markers need additional framing (ALL-CAPS occurrence OR a `!` nearby OR
# explicit "this is X" phrasing) to fire — protects against casual mentions.
_AUTO_URGENT_WEAK = re.compile(
    r"\b(urgent|critical\s+bug|broken|on\s+fire|fire)\b",
    re.IGNORECASE,
)


def _detect_urgency(*texts: str) -> str | None:
    """Return the matched keyword if any source text expresses urgency, else None.

    Strong markers fire unconditionally. Weak markers require either an
    ALL-CAPS occurrence of the same word OR an exclamation mark within ~40
    chars OR an explicit framing phrase ("this is urgent", "it's urgent")."""
    for t in texts:
        if not t:
            continue
        m = _AUTO_URGENT_STRONG.search(t)
        if m:
            return m.group(0).lower()
    for t in texts:
        if not t:
            continue
        m = _AUTO_URGENT_WEAK.search(t)
        if not m:
            continue
        word = m.group(0)
        # Strong framing: ALL-CAPS form of the word anywhere in this text
        if re.search(rf"\b{re.escape(word)}\b", t) and word.upper() in t:
            return word.lower()
        # Or `!` within 40 chars of the match
        start, end = m.span()
        window = t[max(0, start - 40): end + 40]
        if "!" in window:
            return word.lower()
        # Or explicit "this/it is X" framing
        if re.search(rf"\b(this|it|that)('?s|\s+is)\s+(an?\s+|so\s+|really\s+)?{re.escape(word)}\b", t, re.IGNORECASE):
            return word.lower()
    return None


def _ensure_super_urgent_col(d: dict) -> bool:
    """Insert the super-urgent column at position 0 if missing. Returns True iff
    a new column was created. Idempotent: re-runs are no-ops."""
    cols = d.setdefault("columns", [])
    if any(c.get("id") == "super-urgent" for c in cols):
        return False
    cols.insert(0, {
        "id": "super-urgent",
        "name": "🚨 SUPER URGENT",
        "kind": "todo",
        "stackUnder": None,
    })
    return True


def _ensure_ideas_col(d: dict) -> bool:
    """Insert the ideas column if missing. Returns True iff a new column was
    created. Idempotent. Inserted after backlog if backlog exists, else at
    the head of todo-kind columns."""
    cols = d.setdefault("columns", [])
    if any(c.get("id") == "ideas" for c in cols):
        return False
    insert_at = 0
    for i, c in enumerate(cols):
        if c.get("id") == "backlog":
            insert_at = i + 1
            break
    cols.insert(insert_at, {
        "id": "ideas",
        "name": "💡 Ideas",
        "kind": "todo",
        "stackUnder": None,
    })
    return True


def _log_auto_urgent(board: Path, card_num: int, keyword: str, created_col: bool) -> None:
    """Best-effort telemetry: append one event to events.jsonl. Silent on failure
    so card.py adds never break on telemetry hiccups."""
    try:
        ev = {
            "ts": now_iso(),
            "trigger": f"trigger-keyword:{keyword}",
            "project": str(board.parent.resolve()),
            "card_num": card_num,
            "writes": {"cards_added": 1, "auto_urgent_col_created": created_col},
            "notes": f"auto-urgent fired on keyword '{keyword}'",
        }
        log_script = SKILL_DIR / "scripts" / "log_event.py"
        if log_script.is_file():
            subprocess.run(
                ["python3", str(log_script), "--event", json.dumps(ev)],
                timeout=2, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


# ===== subtask tree helpers =====

def find_subtask(nodes: list, sid: str):
    for st in nodes:
        if st.get("id") == sid:
            return st, nodes
        if st.get("children"):
            r = find_subtask(st["children"], sid)
            if r:
                return r
    return None


def new_subtask_id(card: dict) -> str:
    """Generate a stable-ish subtask id within a card."""
    code = (card.get("code") or card.get("id") or "x").lower().replace("c-", "")
    code = code.replace("_", "-")
    existing = set()
    def walk(nodes):
        for st in nodes:
            existing.add(st.get("id", ""))
            walk(st.get("children", []))
    walk(card.get("subtasks", []))
    i = 1
    while True:
        sid = f"s-{code}-{i}"
        if sid not in existing:
            return sid
        i += 1


__all__ = [
    "find_board", "load", "_auth_headers", "_verify_port_owns_board",
    "_resolve_server_url", "_try_post_to_server", "_write_direct", "atomic_save",
    "find_card", "now_iso", "maybe_stdin", "slugify", "_taxonomy_names",
    "_check_tags", "_detect_urgency", "_ensure_super_urgent_col",
    "_ensure_ideas_col", "_log_auto_urgent", "find_subtask", "new_subtask_id",
    "SKILL_DIR", "REGEN_SCRIPT", "_boardio",
]
