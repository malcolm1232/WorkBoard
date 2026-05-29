#!/usr/bin/env python3
"""board card CLI — mutate board.json without writing dict literals every time.

Handles the boilerplate (load, mutate, bump rev, set savedAt/savedBy='claude',
atomic write, regen index.json) so callers just say what they want changed.

All long-text fields (origin, notes, writeup) accept either a literal flag
value OR `--<field>-stdin` to read from stdin (avoids shell-quoting pain).

Usage examples (run from any cwd; --board defaults to ./board/board.json
walking up the tree):

    # Add a new card to the Ideas column
    card.py add --code BOARD-EVOLVE --column ideas --priority low \\
      --title "Let users self-evolve the skill" \\
      --origin "User asked: ..." --link c-board-v3

    # Move card to Done with a writeup
    card.py move 66 done --writeup-stdin <<< "Shipped foo. Verified bar."

    # Update fields
    card.py update 14 --priority critical --add-tag urgent

    # Subtask ops
    card.py subtask add 65 "Eyeball in Safari + Firefox"
    card.py subtask done 65 s-v3-5
    card.py subtask rm 65 s-v3-5

    # Bidirectional link (also unlink)
    card.py link 66 65
    card.py unlink 66 65

    # Quick read (compact)
    card.py show 65
    card.py list --column inprogress

THE 5 CANONICAL LIFECYCLE TRANSITIONS (see VISION.md §4):
    1. CREATE              card.py add --title "..." --column task --priority mid
    2. BEGIN               card.py move <ref> inprogress
    3. SHIP                card.py move <ref> done --writeup "..."
    4. REOPEN-AS-BUG       card.py bug <ref>                (Done → IP + 'bug' tag)
    5. REOPEN-AS-IMPROVE   card.py improve <ref> "..."     (Done → IP + new subtask)

Plus the end-to-end wrappers:
    card.py sim                 — task → ip → done (canonical happy path)
    card.py sim --with-bug      — task → ip → done → reopen → ip → done
"""
from __future__ import annotations

import argparse
import copy
import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REGEN_SCRIPT = SKILL_DIR / "scripts" / "regen_index.py"
# Default fallback if no server is found by path-match. Override via BOARD_SERVER.
DEFAULT_SERVER_URL = "http://127.0.0.1:7891"

# Ensure scripts/ is importable for the sibling _boardio helper even if card.py
# is imported rather than run as a script (when run, its dir is sys.path[0]).
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
import _boardio  # noqa: E402  (write-safety: flock + rolling backups)

# Set True by main() while it holds the cross-process file lock for the
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


def _verify_port_owns_board(port: int, want_dir: str) -> bool:
    """Health-ping a single port and confirm its `board` field matches `want_dir`.
    Returns False on any failure (timeout, wrong board, dead port)."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=0.4
        ) as r:
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
            headers={"Content-Type": "application/json"},
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


def _check_tags(tags: list[str], d: dict, force: bool) -> list[str]:
    """Filter tags against board.json taxonomy. Unknown tags are blocked unless
    --force, with a close-match suggestion printed to stderr. Returns the
    accepted tag list. Empty taxonomy = pass-through (back-compat)."""
    taxonomy = _taxonomy_names(d)
    if not taxonomy:
        return tags
    accepted = []
    for t in tags or []:
        if t in taxonomy:
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


# ===== commands =====

def cmd_add(args, d, board):
    if args.id:
        if any(c.get("id") == args.id for c in d["cards"]):
            sys.exit(f"error: id '{args.id}' already exists")
        cid = args.id
    else:
        slug = slugify(args.code or args.title)
        cid = f"c-{slug}"
        # disambiguate if needed
        if any(c.get("id") == cid for c in d["cards"]):
            n = 2
            while any(c.get("id") == f"{cid}-{n}" for c in d["cards"]):
                n += 1
            cid = f"{cid}-{n}"

    origin = maybe_stdin(args.origin, args.origin_stdin) or ""
    notes  = maybe_stdin(args.notes, args.notes_stdin)   or ""
    writeup= maybe_stdin(args.writeup, args.writeup_stdin) or ""

    now = now_iso()
    # --created-at override: use the provided ISO ts as createdAt (and doneAt
    # for cards landing directly in done). updatedAt stays = now since the
    # card row was actually written now.
    created = getattr(args, "created_at", None) or now
    tags = _check_tags(args.tag or [], d, getattr(args, "force", False))

    # Auto-urgent (#85): detect urgency keywords in title+origin and route to
    # the SUPER URGENT column with critical priority. --urgent forces; --no-auto-urgent skips.
    auto_urgent_kw = None
    auto_urgent_col_created = False
    if getattr(args, "urgent", False):
        auto_urgent_kw = "--urgent"
    elif not getattr(args, "no_auto_urgent", False):
        auto_urgent_kw = _detect_urgency(args.title, origin)
    target_col = args.column
    target_prio = args.priority
    if auto_urgent_kw:
        auto_urgent_col_created = _ensure_super_urgent_col(d)
        target_col = "super-urgent"
        if target_prio not in ("critical",):
            target_prio = "critical"

    # Auto-card (#100): --auto signals intent-detected creation. Defaults the
    # column to 'ideas' when caller didn't override, ensures the col exists,
    # and stamps meta.autoCreated so board.html can pop an undo toast.
    auto_card = bool(getattr(args, "auto", False))
    auto_card_col_created = False
    if auto_card and not auto_urgent_kw:
        if args.column == "backlog":  # caller didn't override the default
            target_col = "ideas"
        if target_col == "ideas":
            auto_card_col_created = _ensure_ideas_col(d)

    card = {
        "num": d["nextNum"],
        "id": cid,
        "code": args.code or "",
        "priority": target_prio,
        "title": args.title,
        "column": target_col,
        "tags": tags,
        "origin": origin,
        "notes": notes,
        "writeup": writeup,
        "createdAt": created,
        "updatedAt": now,
        "doneAt": created if target_col == "done" else None,
        "lastTouchedSubtask": None,
        "linkedCards": [],
        "subtasks": [],
    }
    if auto_card:
        card["meta"] = {
            "autoCreated": True,
            "autoSource": (getattr(args, "auto_source", None) or "").strip(),
        }
    d["cards"].append(card)
    d["nextNum"] += 1

    for target_ref in (args.link or []):
        other = find_card(d, target_ref)
        if other["id"] != cid:
            card["linkedCards"].append(other["id"])
            other.setdefault("linkedCards", [])
            if cid not in other["linkedCards"]:
                other["linkedCards"].append(cid)
            other["updatedAt"] = now

    # #254 — a card born directly into In Progress is active work too (parity
    # with the UI's handleCardAdded → applyActiveWorkTransition).
    _set_active_work(d, card, "", target_col)
    rev = atomic_save(board, d)
    if auto_urgent_kw:
        _log_auto_urgent(board, card["num"], auto_urgent_kw, auto_urgent_col_created)
        col_note = " (🚨 col created)" if auto_urgent_col_created else ""
        print(f"+ #{card['num']} {card['code'] or card['id']} → {target_col}{col_note} "
              f"[auto-urgent: '{auto_urgent_kw}'] (rev {rev})")
    elif auto_card:
        col_note = " (💡 col created)" if auto_card_col_created else ""
        src = (getattr(args, "auto_source", None) or "").strip()
        src_note = f" [auto-card: '{src}']" if src else " [auto-card]"
        print(f"+ #{card['num']} {card['code'] or card['id']} → {target_col}{col_note}"
              f"{src_note} (rev {rev})")
    else:
        print(f"+ #{card['num']} {card['code'] or card['id']} → {target_col} (rev {rev})")


def cmd_update(args, d, board):
    c = find_card(d, args.ref)
    changed = []
    if args.title is not None:    c["title"] = args.title;       changed.append("title")
    if args.code is not None:     c["code"] = args.code;         changed.append("code")
    if args.priority is not None: c["priority"] = args.priority; changed.append("priority")
    if args.column is not None:
        c["column"] = args.column
        if args.column == "done" and not c.get("doneAt"):
            c["doneAt"] = now_iso()
        changed.append("column")

    for field, lit, sflag in [("origin", args.origin, args.origin_stdin),
                              ("notes", args.notes, args.notes_stdin),
                              ("writeup", args.writeup, args.writeup_stdin)]:
        v = maybe_stdin(lit, sflag)
        if v is not None:
            c[field] = v
            changed.append(field)

    for t in _check_tags(args.add_tag or [], d, getattr(args, "force", False)):
        c.setdefault("tags", [])
        if t not in c["tags"]:
            c["tags"].append(t)
            changed.append(f"+tag:{t}")
    for t in (args.rm_tag or []):
        if t in c.get("tags", []):
            c["tags"].remove(t)
            changed.append(f"-tag:{t}")

    # #102 BOARD-AUTO-LINK — linkedFiles drive the PreToolUse flash hook.
    # Paths are normalised to absolute form so basename + absolute hits both
    # work against the same canonical entry.
    for fp in (getattr(args, "add_linked_file", None) or []):
        fp_abs = str(Path(fp).expanduser().resolve())
        c.setdefault("linkedFiles", [])
        if fp_abs not in c["linkedFiles"]:
            c["linkedFiles"].append(fp_abs)
            changed.append(f"+file:{Path(fp_abs).name}")
    for fp in (getattr(args, "rm_linked_file", None) or []):
        fp_abs = str(Path(fp).expanduser().resolve())
        before = list(c.get("linkedFiles") or [])
        c["linkedFiles"] = [x for x in before if x != fp_abs]
        if len(c["linkedFiles"]) != len(before):
            changed.append(f"-file:{Path(fp_abs).name}")

    if not changed:
        sys.exit("nothing to update — pass at least one field")
    c["updatedAt"] = now_iso()
    rev = atomic_save(board, d)
    print(f"~ #{c['num']} {','.join(changed)} (rev {rev})")


def _autocheck_subtasks(nodes, ts):
    """Recursively mark all open subtasks done (with doneAt=ts)."""
    for st in nodes:
        if not st.get("done"):
            st["done"] = True
            st["doneAt"] = ts
        if st.get("children"):
            _autocheck_subtasks(st["children"], ts)


def _find_subtask_anywhere(nodes, sid):
    """Locate subtask by id in the tree; return the node or None."""
    for st in nodes:
        if st.get("id") == sid:
            return st
        kid = _find_subtask_anywhere(st.get("children") or [], sid)
        if kid:
            return kid
    return None


def _set_active_work(d, card, old_col, new_col):
    """#254 — track the single active-work card = the last one MOVED INTO
    In Progress. Persisted in board.json as activeWorkId so the pulse + top-pin
    survive a page refresh. A card merely SITTING in In Progress never becomes
    active (only a real transition does); moving the active card out clears it.
    This is the old live-transition definition, made persistent."""
    if new_col == "inprogress" and old_col != "inprogress":
        d["activeWorkId"] = card["id"]
    elif old_col == "inprogress" and new_col != "inprogress" \
            and d.get("activeWorkId") == card["id"]:
        d["activeWorkId"] = None


def cmd_move(args, d, board):
    c = find_card(d, args.ref)
    old = c["column"]
    c["column"] = args.column
    if args.column == "done":
        ts = now_iso()
        c["doneAt"] = c.get("doneAt") or ts
        # Auto-strip the 'bug' tag on done — regression is fixed.
        if "bug" in (c.get("tags") or []):
            c["tags"] = [t for t in c["tags"] if t != "bug"]
        # #188 — every ship is a SUBTASK in the card's cycle history.
        # No force-auto-check across the tree (Done can sit with open
        # subtasks; that's a deliberate "shipped 1/5" state).
        c.setdefault("subtasks", [])
        if not c["subtasks"]:
            # First ship of this card — append a single cycle marker.
            sid = new_subtask_id(c)
            c["subtasks"].append({
                "id": sid, "text": "☑ initial ship",
                "done": True, "doneAt": ts,
                "createdAt": ts, "children": [],
            })
            c["lastTouchedSubtask"] = sid
        else:
            # Subsequent ship — close ONLY the cycle subtask in flight
            # (lastTouchedSubtask). Sibling subtasks stay in whatever
            # state the user left them.
            sid = c.get("lastTouchedSubtask")
            st = _find_subtask_anywhere(c["subtasks"], sid) if sid else None
            if st and not st.get("done"):
                st["done"] = True
                st["doneAt"] = ts
    elif args.column != "done" and old == "done":
        c["doneAt"] = None  # un-done
    wu = maybe_stdin(args.writeup, args.writeup_stdin)
    if wu is not None:
        c["writeup"] = wu
    c["updatedAt"] = now_iso()
    _set_active_work(d, c, old, args.column)
    rev = atomic_save(board, d)
    suffix = " + writeup" if wu is not None else ""
    print(f"→ #{c['num']} {old} → {args.column}{suffix} (rev {rev})")


def cmd_fly(args, d, board):
    """FLY transition — atomic single-hop column change with side-effect
    shortcuts and a built-in animation pause so chained flies don't race
    the browser's simulateUserDragMove (~320ms).

    `move` mutates data. `fly` mutates data + asserts the animation contract.

    Side-effect flags (apply BEFORE the hop):
      --bug REASON     → add 'bug' tag + 🐞 fix-bug subtask
      --improve TEXT   → add improvement subtask
      --subtask TEXT   → add plain subtask
      --note TEXT      → append to notes
      --writeup TEXT   → set writeup (typical for hops into 'done')

    Pause:
      --pause-ms N     → sleep N ms AFTER the save (default 400)
    """
    c = find_card(d, args.ref)
    old = c["column"]
    ts = now_iso()

    # Side-effects in order: note, plain subtask, bug, improve.
    if args.note:
        existing = (c.get("notes") or "").rstrip()
        c["notes"] = (existing + "\n" + args.note) if existing else args.note
    if args.subtask:
        c.setdefault("subtasks", [])
        sid = new_subtask_id(c)
        c["subtasks"].append({
            "id": sid, "text": args.subtask, "done": False,
            "createdAt": ts, "children": [],
        })
        c["lastTouchedSubtask"] = sid
    if args.bug:
        c.setdefault("tags", [])
        if "bug" not in c["tags"]:
            c["tags"].append("bug")
        c.setdefault("subtasks", [])
        sid = new_subtask_id(c)
        reason = (args.bug or "").strip()
        text = f"🐞 fix bug: {reason}" if reason else "🐞 fix bug"
        c["subtasks"].append({
            "id": sid, "text": text, "done": False,
            "createdAt": ts, "children": [],
        })
        c["lastTouchedSubtask"] = sid
    if args.improve:
        c.setdefault("subtasks", [])
        sid = new_subtask_id(c)
        c["subtasks"].append({
            "id": sid, "text": args.improve, "done": False,
            "createdAt": ts, "children": [],
        })
        c["lastTouchedSubtask"] = sid

    # The hop. Mirrors cmd_move's done-semantics so cycle-history (#188) and
    # bug-tag auto-strip stay consistent across both verbs.
    c["column"] = args.column
    if args.column == "done":
        c["doneAt"] = c.get("doneAt") or ts
        if "bug" in (c.get("tags") or []):
            c["tags"] = [t for t in c["tags"] if t != "bug"]
        c.setdefault("subtasks", [])
        if not c["subtasks"]:
            sid = new_subtask_id(c)
            c["subtasks"].append({
                "id": sid, "text": "☑ initial ship",
                "done": True, "doneAt": ts,
                "createdAt": ts, "children": [],
            })
            c["lastTouchedSubtask"] = sid
        else:
            sid = c.get("lastTouchedSubtask")
            st = _find_subtask_anywhere(c["subtasks"], sid) if sid else None
            if st and not st.get("done"):
                st["done"] = True
                st["doneAt"] = ts
    elif old == "done":
        c["doneAt"] = None

    if args.writeup:
        c["writeup"] = args.writeup

    c["updatedAt"] = now_iso()
    _set_active_work(d, c, old, args.column)
    rev = atomic_save(board, d)

    badge = " 🐞" if args.bug else (" ✨" if args.improve else "")
    suffix = " + writeup" if args.writeup else ""
    print(f"✈ #{c['num']} {old} → {args.column}{badge}{suffix} (rev {rev})")

    if args.pause_ms > 0:
        time.sleep(args.pause_ms / 1000.0)


# ═════════════════════════════════════════════════════════════════════
# LIFECYCLE — DO NOT BREAK
# Canonical Claude-task lifecycle wrapped as a single command. The
# orchestration here pairs with the browser-side animation contract
# (window.runLifecycle() in board.html). When adding features, run
# `card.py sim` to verify the end-to-end visual is intact.
# ═════════════════════════════════════════════════════════════════════
def cmd_improve(args, d, board):
    """IMPROVE transition. Done → In Progress + add a new subtask.

    The 5th canonical lifecycle verb (see VISION.md §4). Use this when a
    shipped card needs an enhancement (not a regression — for regressions
    use `card.py bug`). The new subtask captures *what's being added*;
    the parent card stays the same card across improvements (VISION.md:
    'subtasks tree out inside the card, parent never leaves').
    """
    c = find_card(d, args.ref)
    old = c["column"]
    c["column"] = "inprogress"
    c["doneAt"] = None
    c.setdefault("subtasks", [])
    sid = new_subtask_id(c)
    c["subtasks"].append({
        "id": sid,
        "text": args.text,
        "done": False,
        "createdAt": now_iso(),
        "children": [],
    })
    c["lastTouchedSubtask"] = sid
    c["updatedAt"] = now_iso()
    _set_active_work(d, c, old, "inprogress")
    rev = atomic_save(board, d)
    print(f"✨ #{c['num']} {old} → inprogress (+subtask {sid}) (rev {rev})")


def cmd_bug(args, d, board):
    """REOPEN-AS-BUG transition. Done → In Progress + 'bug' tag + bug
    cycle subtask. The 4th canonical lifecycle verb (see VISION.md §4).

    Same effect as the modal's '🐞 Reopen as bug' button: card moves back
    to In Progress, doneAt clears, 'bug' tag added (idempotent), AND a
    new open subtask "🐞 fix bug[: <reason>]" is appended so the bug
    cycle is first-class history (per #188). The 'bug' tag is auto-
    stripped again when the card next lands in done (regression fixed);
    the bug-cycle subtask gets closed by the next ship and stays as
    permanent evidence "this card had a regression".
    """
    c = find_card(d, args.ref)
    if c["column"] == "inprogress" and "bug" in (c.get("tags") or []):
        sys.exit(f"error: #{c['num']} is already an open bug")
    old = c["column"]
    c["column"] = "inprogress"
    c["doneAt"] = None
    c.setdefault("tags", [])
    if "bug" not in c["tags"]:
        c["tags"].append("bug")
    # #188 — bug cycle = a subtask.
    c.setdefault("subtasks", [])
    sid = new_subtask_id(c)
    reason = (args.reason or "").strip()
    text = f"🐞 fix bug: {reason}" if reason else "🐞 fix bug"
    c["subtasks"].append({
        "id": sid, "text": text,
        "done": False,
        "createdAt": now_iso(), "children": [],
    })
    c["lastTouchedSubtask"] = sid
    c["updatedAt"] = now_iso()
    _set_active_work(d, c, old, "inprogress")
    rev = atomic_save(board, d)
    print(f"🐞 #{c['num']} {old} → inprogress (+bug tag, +subtask {sid}) (rev {rev})")


# ===== auto-ship (#101 Phase 3b) =====

def _find_git_root(start: Path) -> Path:
    """Walk up from start looking for a .git dir/file. Returns start if none."""
    p = start.resolve()
    for _ in range(8):
        g = p / ".git"
        if g.is_dir() or g.is_file():
            return p
        if p.parent == p:
            break
        p = p.parent
    return start.resolve()


def _git_log_since(since_ref: str, cwd: Path) -> list[tuple[str, str]]:
    """git log <since_ref>..HEAD → [(short_sha, subject), ...] oldest-first."""
    try:
        out = subprocess.run(
            ["git", "log", f"{since_ref}..HEAD", "--format=%h\t%s", "--reverse"],
            cwd=str(cwd), capture_output=True, text=True, timeout=5, check=True,
        )
    except Exception:
        return []
    rows = []
    for ln in out.stdout.strip().splitlines():
        if "\t" in ln:
            sha, subj = ln.split("\t", 1)
            rows.append((sha.strip(), subj.strip()))
    return rows


def _score_card_against_commits(card: dict, commits: list[tuple[str, str]]) -> tuple[int, list[str]]:
    """Score how strongly a card matches a list of commit subjects.

    Code exact match     = 3 pts
    #num marker          = 2 pts
    long title token     = 1 pt each (token >= 5 chars, max 3 tokens counted)

    Returns (total_score, matched_sha_list). A score >= 2 is treated as a
    confident match (either code+anything, OR #num+anything)."""
    score = 0
    hits: list[str] = []
    code = (card.get("code") or "").upper().strip()
    num_marker = f"#{card.get('num')}"
    title = (card.get("title") or "").lower()
    title_tokens = [w for w in title.split() if len(w) >= 5][:3]
    for sha, subj in commits:
        s = 0
        su = subj.upper()
        sl = subj.lower()
        if code and code in su:
            s += 3
        if num_marker in subj:
            s += 2
        for w in title_tokens:
            if w in sl:
                s += 1
        if s:
            score += s
            hits.append(sha)
    return score, hits


def _auto_ship_writeup(card: dict, commits: list[tuple[str, str]], hits: list[str], extra: str | None) -> str:
    """Build the writeup body for an auto-ship: one-line header naming the
    matched SHAs, then a bullet list of relevant commit subjects, then the
    optional extra prose."""
    relevant = [(s, sub) for s, sub in commits if not hits or s in hits]
    if not relevant:
        relevant = commits[-3:]  # last 3 commits as soft fallback
    sha_list = ", ".join(hits) if hits else relevant[-1][0]
    lines = [f"Shipped in {sha_list}.", ""]
    for sha, subj in relevant:
        lines.append(f"  {sha}  {subj}")
    if extra:
        lines.append("")
        lines.append(extra.strip())
    return "\n".join(lines)


def cmd_auto_ship(args, d, board):
    """#101 BOARD-AUTO-MOVE: auto-promote inprogress cards to done using git log.

    Two modes:
      Scan mode (no ref):  scan inprogress cards, score matches against commits
                           in <since-ref>..HEAD, print candidate table.
      Ship mode (ref):     move that card to done with an auto-generated writeup
                           assembled from the matching commits.

    Default is dry-run preview. Pass --apply to actually move."""
    git_root = _find_git_root(board.parent)
    commits = _git_log_since(args.since_ref, cwd=git_root)
    if not commits:
        sys.exit(f"no commits in range {args.since_ref}..HEAD (git_root={git_root})")

    # SCAN mode
    if not args.ref:
        inprog = [c for c in d["cards"] if c.get("column") == "inprogress"]
        if not inprog:
            print("(no cards in inprogress)")
            return
        rows = []
        for c in inprog:
            score, hits = _score_card_against_commits(c, commits)
            if score >= 2:
                rows.append((c, score, hits))
        rows.sort(key=lambda r: (-r[1], r[0]["num"]))
        print(f"# auto-ship candidates ({args.since_ref}..HEAD, {len(commits)} commits)")
        if not rows:
            print("(no inprogress cards match recent commits)")
            return
        for c, score, hits in rows:
            code = c.get("code") or c.get("id")
            print(f"  #{c['num']:>3} score={score:<2}  {code:<22} {c.get('title','')[:54]}")
            for sha in hits:
                subj = next((s for x, s in commits if x == sha), "")
                print(f"        ↳ {sha}  {subj[:80]}")
        print(f"\n→ to ship one: card.py auto-ship <num> --since-ref {args.since_ref} --apply")
        return

    # SHIP mode
    c = find_card(d, args.ref)
    if c.get("column") == "done" and not args.force:
        sys.exit(f"#{c['num']} already in done (use --force to re-ship)")
    score, hits = _score_card_against_commits(c, commits)
    writeup = _auto_ship_writeup(c, commits, hits, getattr(args, "writeup_extra", None))

    if not args.apply:
        code = c.get("code") or c.get("id")
        print(f"DRY-RUN: would ship #{c['num']} {code} (score={score}, {len(hits)} commit hits)")
        if score < 2:
            print(f"WARN: low match score — no commit in {args.since_ref}..HEAD obviously mentions this card.")
            print("      Re-run with --apply if you've confirmed by eye, or pick a wider --since-ref.")
        print("--- writeup ---")
        print(writeup)
        print("--- end ---")
        print("(re-run with --apply to actually move)")
        return

    # Apply: mirror the cmd_move done branch.
    old = c["column"]
    c["column"] = "done"
    ts = now_iso()
    c["doneAt"] = c.get("doneAt") or ts
    if "bug" in (c.get("tags") or []):
        c["tags"] = [t for t in c["tags"] if t != "bug"]
    c.setdefault("subtasks", [])
    if not c["subtasks"]:
        sid = new_subtask_id(c)
        c["subtasks"].append({
            "id": sid, "text": "☑ initial ship",
            "done": True, "doneAt": ts,
            "createdAt": ts, "children": [],
        })
        c["lastTouchedSubtask"] = sid
    else:
        sid = c.get("lastTouchedSubtask")
        st = _find_subtask_anywhere(c["subtasks"], sid) if sid else None
        if st and not st.get("done"):
            st["done"] = True
            st["doneAt"] = ts
    c["writeup"] = writeup
    c["updatedAt"] = ts
    _set_active_work(d, c, old, "done")
    rev = atomic_save(board, d)
    print(f"✈ #{c['num']} {old} → done [auto-ship, {len(hits)} commit hits] (rev {rev})")


def cmd_sim(args, d, board):
    try:
        gap_ip, gap_done = (float(x) for x in args.intervals.split(","))
    except Exception:
        sys.exit("--intervals must be 'task→ip,ip→done' seconds, e.g. '2,5'")

    title = args.title or f"SIMULATION {now_iso()[11:19].replace(':', '')}"
    writeup = args.writeup or f"Sim via card.py sim (intervals {args.intervals}s)."

    # Step 1 — CREATE in Task. Reuses cmd_add so the lifecycle exercises
    # exactly the production code path (no shortcut writes).
    add_ns = argparse.Namespace(
        title=title, code="", priority=args.priority, column="task",
        tag=[], link=[], id=None,
        origin=None, origin_stdin=False,
        notes=None,  notes_stdin=False,
        writeup=None, writeup_stdin=False,
    )
    cmd_add(add_ns, d, board)
    num = d["nextNum"] - 1

    # Step 2 — MOVE to In Progress (5s+ default to watch the pulse).
    time.sleep(gap_ip)
    d = load(board)  # reload in case anything else touched the board
    cmd_move(argparse.Namespace(
        ref=str(num), column="inprogress",
        writeup=None, writeup_stdin=False,
    ), d, board)

    # Step 3 — MOVE to Done with auto writeup.
    time.sleep(gap_done)
    d = load(board)
    cmd_move(argparse.Namespace(
        ref=str(num), column="done",
        writeup=writeup, writeup_stdin=False,
    ), d, board)

    # Step 4 (optional) — REOPEN AS BUG: simulate a post-ship regression.
    # Card moves Done → In Progress + gets a 'bugged' tag (visible forever
    # in card history). Then a follow-up move to Done with a fix writeup.
    if args.with_bug:
        time.sleep(gap_done)
        d = load(board)
        # Use the canonical bug verb so the sim exercises the production
        # code path (DO NOT BREAK contract — see VISION.md §4).
        cmd_bug(argparse.Namespace(ref=str(num)), d, board)

        time.sleep(gap_ip)
        d = load(board)
        cmd_move(argparse.Namespace(
            ref=str(num), column="done",
            writeup=f"Bug fixed and reshipped. {writeup}",
            writeup_stdin=False,
        ), d, board)

    print(f"✓ sim complete: #{num}")


def cmd_subtask(args, d, board):
    c = find_card(d, args.ref)
    c.setdefault("subtasks", [])
    touched_sid = None  # the subtask id this op touched — used by #188 ship logic.

    if args.op == "add":
        sid = new_subtask_id(c)
        st = {"id": sid, "text": args.text, "done": False, "collapsed": False, "children": []}
        if args.parent:
            r = find_subtask(c["subtasks"], args.parent)
            if not r:
                sys.exit(f"error: no subtask '{args.parent}' under #{c['num']}")
            r[0].setdefault("children", []).append(st)
        else:
            c["subtasks"].append(st)
        touched_sid = sid
        action = f"+ {sid}: {args.text[:60]}"

    elif args.op in ("done", "undone"):
        r = find_subtask(c["subtasks"], args.sid)
        if not r:
            sys.exit(f"error: no subtask '{args.sid}' under #{c['num']}")
        r[0]["done"] = (args.op == "done")
        touched_sid = args.sid
        action = f"{'✓' if args.op == 'done' else '○'} {args.sid}"

    elif args.op == "rm":
        r = find_subtask(c["subtasks"], args.sid)
        if not r:
            sys.exit(f"error: no subtask '{args.sid}' under #{c['num']}")
        r[1].remove(r[0])
        action = f"- {args.sid}"

    else:
        sys.exit(f"unknown subtask op: {args.op}")

    c["updatedAt"] = now_iso()
    if touched_sid:
        c["lastTouchedSubtask"] = touched_sid
    rev = atomic_save(board, d)
    print(f"#{c['num']} subtask {action} (rev {rev})")


def cmd_link(args, d, board):
    a = find_card(d, args.a)
    b = find_card(d, args.b)
    if a["id"] == b["id"]:
        sys.exit("error: can't link a card to itself")
    a.setdefault("linkedCards", [])
    b.setdefault("linkedCards", [])
    if args.op == "link":
        added = []
        if b["id"] not in a["linkedCards"]: a["linkedCards"].append(b["id"]); added.append(f"#{a['num']}→#{b['num']}")
        if a["id"] not in b["linkedCards"]: b["linkedCards"].append(a["id"]); added.append(f"#{b['num']}→#{a['num']}")
        msg = "linked " + (", ".join(added) if added else "(already linked)")
    else:
        if b["id"] in a["linkedCards"]: a["linkedCards"].remove(b["id"])
        if a["id"] in b["linkedCards"]: b["linkedCards"].remove(a["id"])
        msg = f"unlinked #{a['num']}↔#{b['num']}"
    now = now_iso()
    a["updatedAt"] = b["updatedAt"] = now
    rev = atomic_save(board, d)
    print(f"{msg} (rev {rev})")


def cmd_column(args, d, board):
    d.setdefault("columns", [])
    if args.op == "list":
        for c in d["columns"]:
            n = sum(1 for k in d.get("cards", []) if k.get("column") == c["id"])
            print(f"  {c['id']:<14} {c.get('kind','-'):<10} {n:>3} cards  {c.get('name','')}")
        return
    if not args.id:
        sys.exit("error: column id required (e.g. `card.py column add consideration 'Consideration'`)")
    if args.op == "add":
        if any(c["id"] == args.id for c in d["columns"]):
            sys.exit(f"error: column id '{args.id}' already exists")
        col = {"id": args.id, "name": args.name or args.id.title(), "kind": args.kind}
        if args.at is None:
            d["columns"].append(col)
        else:
            d["columns"].insert(max(0, min(args.at, len(d["columns"]))), col)
        rev = atomic_save(board, d)
        print(f"+ column {args.id} (rev {rev})")
    elif args.op == "rm":
        before = len(d["columns"])
        d["columns"] = [c for c in d["columns"] if c["id"] != args.id]
        if len(d["columns"]) == before:
            sys.exit(f"error: no column '{args.id}'")
        in_use = [c for c in d.get("cards", []) if c.get("column") == args.id]
        if in_use:
            sys.exit(f"error: column '{args.id}' still has {len(in_use)} cards — move them first")
        rev = atomic_save(board, d)
        print(f"- column {args.id} (rev {rev})")
    elif args.op == "rename":
        col = next((c for c in d["columns"] if c["id"] == args.id), None)
        if not col:
            sys.exit(f"error: no column '{args.id}'")
        if not args.name:
            sys.exit("error: rename needs a new name")
        col["name"] = args.name
        rev = atomic_save(board, d)
        print(f"~ column {args.id} → '{args.name}' (rev {rev})")


def cmd_show(args, d, board):
    c = find_card(d, args.ref)
    print(json.dumps(c, indent=2, ensure_ascii=False))


def cmd_recover(args, d, board):
    """3.5c — list the rolling backups (3.5b) or restore one.

    Restoring writes the chosen backup as the new current board (rev bumped so
    SSE clients animate it). The pre-restore state is itself already a backup,
    so a restore is reversible. Dry-run by default; --apply to commit.
    """
    backups = _boardio.list_backups(board)   # [(rev, Path)] newest-first
    cur_rev = d.get("rev", 0)

    if getattr(args, "rev", None) is None:
        # LIST mode
        if not backups:
            print("no backups yet — they're written to <board>/.backups/ on every save")
            return
        print(f"{'rev':>7}  {'cards':>5}  {'savedAt':<21} savedBy")
        for rev, p in backups:
            try:
                b = json.loads(p.read_text())
                ncards, savedAt, savedBy = len(b.get("cards", [])), b.get("savedAt", "?"), b.get("savedBy", "?")
            except Exception:
                ncards, savedAt, savedBy = "?", "(unreadable)", "?"
            mark = "  ← current" if rev == cur_rev else ""
            print(f"{rev:>7}  {ncards:>5}  {savedAt:<21} {savedBy}{mark}")
        print("\nrestore with:  card.py recover <rev> --apply")
        return

    # RESTORE mode — locate + validate before touching anything.
    target = next((p for rev, p in backups if rev == args.rev), None)
    if target is None:
        avail = ", ".join(str(r) for r, _ in backups) or "none"
        sys.exit(f"error: no backup for rev {args.rev} (available: {avail})")
    try:
        restored = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"error: backup rev {args.rev} is unreadable/corrupt: {e}")
    if not isinstance(restored, dict) or not isinstance(restored.get("cards"), list):
        sys.exit(f"error: backup rev {args.rev} is not a valid board (missing cards[])")

    ncards, cur_cards = len(restored["cards"]), len(d.get("cards", []))
    if not getattr(args, "apply", False):
        print(f"DRY-RUN: would restore rev {args.rev} ({ncards} cards) over "
              f"current rev {cur_rev} ({cur_cards} cards).")
        print("Re-run with --apply to write it. Current state stays in .backups, so it's reversible.")
        return

    # Seed rev from current so atomic_save bumps to cur_rev+1 (a forward rev
    # SSE clients accept) rather than replaying the backup's old, lower rev.
    restored["rev"] = cur_rev
    rev = atomic_save(board, restored)
    print(f"♻ restored backup rev {args.rev} → live as rev {rev} ({ncards} cards). "
          f"Pre-restore state (rev {cur_rev}) remains in .backups.")


# ===== schema migrations (3.5d) =====
# Bump SCHEMA_VERSION and append a migration whenever the card/board shape gains
# a field. Each migration MUST be idempotent (only fill what's missing) so it's
# safe to re-run and safe to apply to a board imported from an older version
# (e.g. a discover-bootstrapped board missing newer fields).
SCHEMA_VERSION = 2

# Canonical per-card fields → default factory/value (callable = fresh instance).
_CARD_DEFAULTS = {
    "code": "", "tags": list, "origin": "", "notes": "", "writeup": "",
    "linkedCards": list, "subtasks": list, "doneAt": None,
    "lastTouchedSubtask": None,
}


def _migrate_v1_card_fields(d):
    """v1 — backfill canonical per-card fields + cross-fill timestamps."""
    changed = 0
    for c in d.get("cards", []):
        for k, v in _CARD_DEFAULTS.items():
            if k not in c:
                c[k] = v() if callable(v) else v
                changed += 1
        if "updatedAt" not in c and c.get("createdAt"):
            c["updatedAt"] = c["createdAt"]; changed += 1
        if "createdAt" not in c and c.get("updatedAt"):
            c["createdAt"] = c["updatedAt"]; changed += 1
    return changed


def _migrate_v2_board_fields(d):
    """v2 — backfill board-level fields (columns, cards, nextNum)."""
    changed = 0
    if "columns" not in d: d["columns"] = []; changed += 1
    if "cards" not in d: d["cards"] = []; changed += 1
    if "nextNum" not in d:
        d["nextNum"] = max((c.get("num", 0) for c in d.get("cards", [])), default=0) + 1
        changed += 1
    return changed


MIGRATIONS = [
    (1, "backfill canonical per-card fields", _migrate_v1_card_fields),
    (2, "backfill board-level fields (columns, cards, nextNum)", _migrate_v2_board_fields),
]


def cmd_migrate(args, d, board):
    """3.5d — apply idempotent schemaVersion migrations. Dry-run by default."""
    cur = d.get("schemaVersion", 0)
    pending = [(v, name, fn) for v, name, fn in MIGRATIONS if v > cur]
    if not pending:
        print(f"schema up to date (schemaVersion {cur}, latest {SCHEMA_VERSION}) — nothing to migrate")
        return

    if not getattr(args, "apply", False):
        print(f"DRY-RUN: schemaVersion {cur} → {SCHEMA_VERSION}, {len(pending)} migration(s) pending:")
        probe = copy.deepcopy(d)
        for v, name, fn in pending:
            n = fn(probe)  # mutates the throwaway copy only
            print(f"  v{v}  {name}  ({n} field(s) would change)")
        print("Re-run with --apply to write. Current state stays in .backups, so it's reversible.")
        return

    total = 0
    for v, name, fn in pending:
        n = fn(d)
        total += n
        print(f"  v{v}  {name}  ({n} field(s) changed)")
    d["schemaVersion"] = SCHEMA_VERSION
    rev = atomic_save(board, d)
    print(f"✓ migrated to schemaVersion {SCHEMA_VERSION} ({total} field(s) backfilled) (rev {rev})")


def cmd_repair_links(args, d, board):
    """3.5e — walk linkedCards and fix integrity: drop dangling (target gone),
    self, and duplicate ids; restore reciprocity for one-sided links (links are
    bidirectional by design). Dry-run by default; idempotent on re-run."""
    cards = d.get("cards", [])
    by_id = {c["id"]: c for c in cards if "id" in c}

    dangling = []   # (num, bad_id)
    selflinks = []  # (num,)
    dupes = []      # (num, dup_id)
    onesided = []   # (num, other_id) — other exists but doesn't link back
    for c in cards:
        seen = set()
        for oid in (c.get("linkedCards") or []):
            if oid == c.get("id"):
                selflinks.append((c.get("num"),)); continue
            if oid in seen:
                dupes.append((c.get("num"), oid)); continue
            seen.add(oid)
            if oid not in by_id:
                dangling.append((c.get("num"), oid)); continue
            if c.get("id") not in (by_id[oid].get("linkedCards") or []):
                onesided.append((c.get("num"), oid))

    total = len(dangling) + len(selflinks) + len(dupes) + len(onesided)
    if total == 0:
        print("links healthy — nothing to repair")
        return

    def _num(oid):
        return f"#{by_id[oid]['num']}" if oid in by_id else f"{oid}(gone)"

    print(f"{total} link issue(s) found:")
    for num, oid in dangling:  print(f"  drop dangling   #{num} → {oid} (no such card)")
    for (num,) in selflinks:   print(f"  drop self-link  #{num} → itself")
    for num, oid in dupes:     print(f"  drop duplicate  #{num} → {_num(oid)}")
    for num, oid in onesided:  print(f"  add reciprocal  {_num(oid)} → #{num} (currently one-sided)")

    if not getattr(args, "apply", False):
        print("\nRe-run with --apply to fix. Current state stays in .backups, so it's reversible.")
        return

    # 1) Clean each list: drop self/dangling/dupes, preserve order.
    for c in cards:
        seen, cleaned = set(), []
        for oid in (c.get("linkedCards") or []):
            if oid == c.get("id") or oid not in by_id or oid in seen:
                continue
            seen.add(oid); cleaned.append(oid)
        c["linkedCards"] = cleaned
    # 2) Restore reciprocity for every surviving link.
    recip = 0
    for c in cards:
        for oid in c["linkedCards"]:
            ol = by_id[oid].setdefault("linkedCards", [])
            if c["id"] not in ol:
                ol.append(c["id"]); recip += 1
    rev = atomic_save(board, d)
    print(f"✓ repaired: {len(dangling)} dangling, {len(selflinks)} self, "
          f"{len(dupes)} dupe dropped; {recip} reciprocal(s) added (rev {rev})")


# ===== prelaunch gate (#91) =====
# Cards in launch-blocking columns/priorities that aren't shipped or blocked.
# Surface these before any public-facing ship: github-repo flip private→public,
# `gh release create`, `npm publish`, marketing send, DNS go-live. Claude calls
# this before launch-shaped actions; SessionStart hook also injects a count.

_LAUNCH_BLOCKING_COLS = ("super-urgent", "mandatory")
_LAUNCH_BLOCKING_PRIOS = ("critical", "mid")


def _prelaunch_open_cards(d: dict) -> list[dict]:
    """Return list of cards that block launch.

    Rule (from card #91): in super-urgent or mandatory column, priority
    critical or mid, not in done/blocked. Sorted: super-urgent first, then
    by priority (critical → mid), then by card num."""
    open_cards = []
    for c in d.get("cards", []):
        col = c.get("column")
        if col not in _LAUNCH_BLOCKING_COLS:
            continue
        if (c.get("priority") or "low") not in _LAUNCH_BLOCKING_PRIOS:
            continue
        open_cards.append(c)
    open_cards.sort(key=lambda c: (
        0 if c.get("column") == "super-urgent" else 1,
        0 if c.get("priority") == "critical" else 1,
        c.get("num", 0),
    ))
    return open_cards


def cmd_prelaunch_check(args, d, board):
    open_cards = _prelaunch_open_cards(d)
    if args.json:
        out = [{
            "num": c["num"],
            "code": c.get("code") or c.get("id"),
            "column": c.get("column"),
            "priority": c.get("priority"),
            "title": c.get("title", ""),
        } for c in open_cards]
        print(json.dumps({"open_count": len(open_cards), "items": out}, indent=2))
    elif args.count:
        print(len(open_cards))
    else:
        if not open_cards:
            print("✅ prelaunch-check: 0 blocking items — clear to launch")
        else:
            print(f"⚠️  prelaunch-check: {len(open_cards)} item(s) still open")
            for c in open_cards:
                col = "SUPER URGENT" if c["column"] == "super-urgent" else "MANDATORY  "
                p = (c.get("priority") or "-")[:1].upper()
                code = c.get("code") or c.get("id")
                print(f"  [{col}] #{c['num']:>3} [{p}] {code:<18} {c.get('title','')[:60]}")
            print()
            print(f"Run `card.py show <num>` for detail, or `card.py fly <num> done` to ship.")
    sys.exit(0 if not open_cards else 9)


def cmd_list(args, d, board):
    cards = d["cards"]
    if args.column:
        cards = [c for c in cards if c.get("column") == args.column]
    if args.priority:
        cards = [c for c in cards if c.get("priority") == args.priority]
    if args.tag:
        cards = [c for c in cards if args.tag in (c.get("tags") or [])]
    for c in cards:
        p = (c.get("priority") or "-")[:1].upper()
        code = c.get("code") or c.get("id")
        print(f"  #{c['num']:>3} [{p}] {c.get('column'):<10} {code:<14} {c.get('title','')[:70]}")
    print(f"({len(cards)} cards)")


# ===== argparse wiring =====

def build_parser():
    ap = argparse.ArgumentParser(prog="card", description="board card CLI")
    ap.add_argument("--board", type=Path, default=None,
                    help="path to board.json (default: ./board/board.json, walks up)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # add
    pa = sub.add_parser("add", help="add a new card")
    pa.add_argument("--title", required=True)
    pa.add_argument("--column", default="backlog",
                    )
    pa.add_argument("--priority", choices=["critical","mid","low"], default=None)
    pa.add_argument("--code", default=None, help="optional short badge like 'FACT9'")
    pa.add_argument("--id", default=None, help="explicit id (default: slug of code/title)")
    pa.add_argument("--tag", action="append", default=None, help="repeat for multiple tags")
    pa.add_argument("--origin", default=None)
    pa.add_argument("--origin-stdin", action="store_true")
    pa.add_argument("--notes", default=None)
    pa.add_argument("--notes-stdin", action="store_true")
    pa.add_argument("--writeup", default=None)
    pa.add_argument("--writeup-stdin", action="store_true")
    pa.add_argument("--link", action="append", default=None, help="ref(s) to link bidirectionally")
    pa.add_argument("--created-at", default=None,
                    help="ISO timestamp to override createdAt (default: now). "
                         "Use when importing historic work so board sorts chronologically.")
    pa.add_argument("--force", action="store_true",
                    help="Accept tags that aren't in board.json tagTaxonomy (otherwise blocked w/ close-match suggestion)")
    pa.add_argument("--urgent", action="store_true",
                    help="Force-route this card to 🚨 SUPER URGENT col with critical priority "
                         "(creates the column if missing).")
    pa.add_argument("--no-auto-urgent", action="store_true",
                    help="Skip urgency-keyword detection in title/origin (#85). Use when "
                         "the words 'urgent/asap/blocker/...' are part of the card content, "
                         "not a real urgency signal.")
    pa.add_argument("--auto", action="store_true",
                    help="Mark this card as auto-created from intent detection (#100). "
                         "Defaults --column to 'ideas' (created if missing) and stamps "
                         "meta.autoCreated so the board pops a 5s Undo toast.")
    pa.add_argument("--auto-source", default=None,
                    help="The user phrase that triggered auto-card (e.g. 'I have an idea:'). "
                         "Stored in meta.autoSource + shown in the Undo toast.")
    pa.set_defaults(fn=cmd_add)

    # update
    pu = sub.add_parser("update", help="patch fields on an existing card")
    pu.add_argument("ref", help="#N or id or code")
    pu.add_argument("--title", default=None)
    pu.add_argument("--code", default=None)
    pu.add_argument("--column", default=None,
                    )
    pu.add_argument("--priority", default=None, choices=["critical","mid","low"])
    pu.add_argument("--origin", default=None)
    pu.add_argument("--origin-stdin", action="store_true")
    pu.add_argument("--notes", default=None)
    pu.add_argument("--notes-stdin", action="store_true")
    pu.add_argument("--writeup", default=None)
    pu.add_argument("--writeup-stdin", action="store_true")
    pu.add_argument("--add-tag", action="append", default=None)
    pu.add_argument("--rm-tag", action="append", default=None)
    pu.add_argument("--add-linked-file", action="append", default=None,
                    help="path to a file this card 'owns' (#102 AUTO-LINK). When the "
                         "PreToolUse hook sees Edit/Write on this path, the board flashes "
                         "this card's border. Paths are normalised to absolute form.")
    pu.add_argument("--rm-linked-file", action="append", default=None,
                    help="remove a linked-file path (matched after the same abs-path normalisation)")
    pu.add_argument("--force", action="store_true",
                    help="Accept tags that aren't in board.json tagTaxonomy")
    pu.set_defaults(fn=cmd_update)

    # move
    pm = sub.add_parser("move", help="change a card's column")
    pm.add_argument("ref")
    pm.add_argument("column")
    pm.add_argument("--writeup", default=None)
    pm.add_argument("--writeup-stdin", action="store_true")
    pm.set_defaults(fn=cmd_move)

    # fly — atomic single-hop with side-effect shortcuts + animation pause
    pfy = sub.add_parser("fly", help="single-hop column change with --bug/--improve/--note shortcuts + animation pause")
    pfy.add_argument("ref")
    pfy.add_argument("column", help="destination column id")
    pfy.add_argument("--bug", metavar="REASON", default=None,
                     help="add 'bug' tag + 🐞 fix-bug subtask")
    pfy.add_argument("--improve", metavar="TEXT", default=None,
                     help="add improvement subtask")
    pfy.add_argument("--subtask", metavar="TEXT", default=None,
                     help="add plain subtask")
    pfy.add_argument("--note", metavar="TEXT", default=None,
                     help="append to notes")
    pfy.add_argument("--writeup", default=None, help="set writeup (typical for done)")
    pfy.add_argument("--pause-ms", type=int, default=400,
                     help="sleep N ms after save (default 400, matches simulateUserDragMove)")
    pfy.set_defaults(fn=cmd_fly)

    # subtask
    ps = sub.add_parser("subtask", help="subtask ops")
    ps.add_argument("op", choices=["add","done","undone","rm"])
    ps.add_argument("ref", help="card ref (#N / id / code)")
    ps.add_argument("text_or_sid", help="text (for add) or subtask id (for done/undone/rm)")
    ps.add_argument("--parent", default=None, help="parent subtask id (for nested add)")
    def _subtask_dispatch(args, d, board):
        if args.op == "add":
            args.text = args.text_or_sid; args.sid = None
        else:
            args.sid = args.text_or_sid; args.text = None
        cmd_subtask(args, d, board)
    ps.set_defaults(fn=_subtask_dispatch)

    # link / unlink
    pl = sub.add_parser("link", help="bidirectionally link two cards")
    pl.add_argument("a"); pl.add_argument("b")
    pl.set_defaults(fn=lambda args, d, board: cmd_link(argparse.Namespace(**vars(args), op="link"), d, board))

    pul = sub.add_parser("unlink", help="remove a bidirectional link")
    pul.add_argument("a"); pul.add_argument("b")
    pul.set_defaults(fn=lambda args, d, board: cmd_link(argparse.Namespace(**vars(args), op="unlink"), d, board))

    # column ops — add/rm/rename. Custom columns emit column-added SSE events
    # so the UI slides them in alongside any subsequent cards.
    pc = sub.add_parser("column", help="column add/rm/rename")
    pc.add_argument("op", choices=["add","rm","rename","list"])
    pc.add_argument("id", nargs="?", help="column id (e.g. 'consideration')")
    pc.add_argument("name", nargs="?", help="display name (for add/rename)")
    pc.add_argument("--kind", default="custom",
                    help="column kind hint (intake|todo|active|blocked|done|custom)")
    pc.add_argument("--at", type=int, default=None,
                    help="position to insert at (0-based); default = append")
    pc.set_defaults(fn=cmd_column)

    # show / list
    psh = sub.add_parser("show", help="print one card as JSON")
    psh.add_argument("ref")
    psh.set_defaults(fn=cmd_show)

    # recover (3.5c) — list rolling backups or restore one
    prc = sub.add_parser("recover", help="list rolling backups, or restore one (3.5c)")
    prc.add_argument("rev", nargs="?", type=int, help="backup rev to restore (omit to list)")
    prc.add_argument("--apply", action="store_true",
                     help="actually write the restore (default: dry-run)")
    prc.set_defaults(fn=cmd_recover)

    # migrate (3.5d) — apply idempotent schemaVersion migrations
    pmg = sub.add_parser("migrate", help="apply schemaVersion migrations (3.5d)")
    pmg.add_argument("--apply", action="store_true",
                     help="actually run the migrations (default: dry-run)")
    pmg.set_defaults(fn=cmd_migrate)

    # repair-links (3.5e) — fix dangling/self/dupe/one-sided linkedCards
    prl = sub.add_parser("repair-links", help="fix linkedCards integrity (3.5e)")
    prl.add_argument("--apply", action="store_true",
                     help="actually apply the fixes (default: dry-run)")
    prl.set_defaults(fn=cmd_repair_links)

    pbug = sub.add_parser("bug", help="reopen a Done card as a bug (Done → In Progress + 'bug' tag + 🐞 fix-bug subtask)")
    pbug.add_argument("ref", help="card num or id")
    pbug.add_argument("--reason", help="optional reason — becomes the bug-fix subtask text")
    pbug.set_defaults(fn=cmd_bug)

    pimp = sub.add_parser("improve", help="add an improvement subtask + reopen (Done → In Progress + new subtask)")
    pimp.add_argument("ref", help="card num or id")
    pimp.add_argument("text", help="subtask text (the improvement)")
    pimp.set_defaults(fn=cmd_improve)

    pas = sub.add_parser("auto-ship",
                         help="auto-promote inprogress cards to done using git log (#101). "
                              "No ref = scan mode (table of candidates); ref + --apply = ship that one.")
    pas.add_argument("ref", nargs="?", default=None,
                     help="card num/code/id to ship (omit for scan mode)")
    pas.add_argument("--since-ref", default="HEAD~1",
                     help="git ref starting bound; commits in <ref>..HEAD are scanned (default HEAD~1)")
    pas.add_argument("--writeup-extra", default=None,
                     help="append this prose to the auto-generated writeup")
    pas.add_argument("--apply", action="store_true",
                     help="actually move the card (default is dry-run preview)")
    pas.add_argument("--force", action="store_true",
                     help="re-ship a card already in done")
    pas.set_defaults(fn=cmd_auto_ship)

    psim = sub.add_parser("sim", help="run canonical lifecycle: task → inprogress → done")
    psim.add_argument("--title", default=None, help="card title (default: auto-named)")
    psim.add_argument("--priority", default="mid", choices=["critical", "mid", "low"])
    psim.add_argument("--intervals", default="2,5",
                      help="seconds between phases: 'task→ip,ip→done' (default '2,5')")
    psim.add_argument("--writeup", default=None, help="custom done writeup (auto if omitted)")
    psim.add_argument("--with-bug", action="store_true",
                      help="after done, reopen as bug (+'bugged' tag), then re-finish")
    psim.set_defaults(fn=cmd_sim)

    pls = sub.add_parser("list", help="list cards (filtered)")
    pls.add_argument("--column", default=None)
    pls.add_argument("--priority", default=None)
    pls.add_argument("--tag", default=None)
    pls.set_defaults(fn=cmd_list)

    # prelaunch-check (#91) — exit 9 if any super-urgent/mandatory items open
    ppl = sub.add_parser("prelaunch-check",
                         help="exit 9 if any super-urgent/mandatory cards still open. "
                              "Run BEFORE any public-facing ship (gh release, npm publish, "
                              "DNS go-live, repo flip private→public).")
    ppl.add_argument("--json", action="store_true", help="emit JSON instead of human text")
    ppl.add_argument("--count", action="store_true", help="emit just the open count (for shell scripts)")
    ppl.set_defaults(fn=cmd_prelaunch_check)

    return ap


def main():
    global _HOLDING_LOCK
    args = build_parser().parse_args()
    board = find_board(args.board)

    # If a server owns this board, writes funnel through it (POST) and the
    # server serializes them — no file lock here (holding it during a POST would
    # deadlock the server's own locked write). If NOT, we write directly, so we
    # hold the file lock across load→dispatch→save: the read is then fresh under
    # the lock and concurrent direct writers can't lose each other's updates.
    server_present = (
        os.environ.get("BOARD_NO_SERVER") != "1"
        and _resolve_server_url(board) is not None
    )
    if server_present:
        d = load(board)
        args.fn(args, d, board)
    else:
        with _boardio.board_lock(board):
            _HOLDING_LOCK = True
            try:
                d = load(board)
                args.fn(args, d, board)
            finally:
                _HOLDING_LOCK = False


if __name__ == "__main__":
    main()
