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
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
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


def _resolve_server_url(board_path: Path) -> str | None:
    """Find the running board server that owns this board.json path.

    Probes localhost ports in [7891, 7900] for /health; matches the one whose
    `board` field equals the parent dir of our board.json. Returns the URL
    on a match, None if no server claims this board (caller falls back to
    direct file write — never to a wrong server).

    Why: card.py used to hardcode :7891. Running it from a different project's
    board would POST that project's data to whatever server was on :7891 —
    silently clobbering the wrong board. This routes by board-path, not port.
    """
    env_url = os.environ.get("BOARD_SERVER")
    if env_url:
        return env_url  # explicit override wins
    want = str(board_path.parent.resolve())
    for port in range(7891, 7901):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=0.4
            ) as r:
                if r.status != 200:
                    continue
                info = json.loads(r.read())
                got = info.get("board")
                if got and Path(got).resolve() == Path(want).resolve():
                    return f"http://127.0.0.1:{port}"
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError,
                json.JSONDecodeError):
            continue
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


def atomic_save(p: Path, d: dict, regen: bool = True) -> int:
    """Bump rev, set savedAt/savedBy=claude.

    Preferred path: POST to the running board server so SSE clients animate
    the change in real-time. Fallback: write the file directly + regen index.
    Returns new rev.
    """
    d["rev"] = d.get("rev", 0) + 1
    d["savedAt"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    d["savedBy"] = "claude"

    if _try_post_to_server(d, p):
        return d["rev"]

    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".board.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise
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
    card = {
        "num": d["nextNum"],
        "id": cid,
        "code": args.code or "",
        "priority": args.priority,
        "title": args.title,
        "column": args.column,
        "tags": args.tag or [],
        "origin": origin,
        "notes": notes,
        "writeup": writeup,
        "createdAt": now,
        "updatedAt": now,
        "doneAt": now if args.column == "done" else None,
        "lastTouchedSubtask": None,
        "linkedCards": [],
        "subtasks": [],
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

    rev = atomic_save(board, d)
    print(f"+ #{card['num']} {card['code'] or card['id']} → {args.column} (rev {rev})")


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

    for t in (args.add_tag or []):
        c.setdefault("tags", [])
        if t not in c["tags"]:
            c["tags"].append(t)
            changed.append(f"+tag:{t}")
    for t in (args.rm_tag or []):
        if t in c.get("tags", []):
            c["tags"].remove(t)
            changed.append(f"-tag:{t}")

    if not changed:
        sys.exit("nothing to update — pass at least one field")
    c["updatedAt"] = now_iso()
    rev = atomic_save(board, d)
    print(f"~ #{c['num']} {','.join(changed)} (rev {rev})")


def cmd_move(args, d, board):
    c = find_card(d, args.ref)
    old = c["column"]
    c["column"] = args.column
    if args.column == "done":
        c["doneAt"] = c.get("doneAt") or now_iso()
    elif args.column != "done" and old == "done":
        c["doneAt"] = None  # un-done
    wu = maybe_stdin(args.writeup, args.writeup_stdin)
    if wu is not None:
        c["writeup"] = wu
    c["updatedAt"] = now_iso()
    rev = atomic_save(board, d)
    suffix = " + writeup" if wu is not None else ""
    print(f"→ #{c['num']} {old} → {args.column}{suffix} (rev {rev})")


# ═════════════════════════════════════════════════════════════════════
# LIFECYCLE — DO NOT BREAK
# Canonical Claude-task lifecycle wrapped as a single command. The
# orchestration here pairs with the browser-side animation contract
# (window.runLifecycle() in board.html). When adding features, run
# `card.py sim` to verify the end-to-end visual is intact.
# ═════════════════════════════════════════════════════════════════════
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
    print(f"✓ sim complete: #{num}")


def cmd_subtask(args, d, board):
    c = find_card(d, args.ref)
    c.setdefault("subtasks", [])

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
        action = f"+ {sid}: {args.text[:60]}"

    elif args.op in ("done", "undone"):
        r = find_subtask(c["subtasks"], args.sid)
        if not r:
            sys.exit(f"error: no subtask '{args.sid}' under #{c['num']}")
        r[0]["done"] = (args.op == "done")
        action = f"{'✓' if args.op == 'done' else '○'} {args.sid}"

    elif args.op == "rm":
        r = find_subtask(c["subtasks"], args.sid)
        if not r:
            sys.exit(f"error: no subtask '{args.sid}' under #{c['num']}")
        r[1].remove(r[0])
        action = f"- {args.sid}"

    else:
        sys.exit(f"unknown subtask op: {args.op}")

    c["updatedAt"] = c["lastTouchedSubtask"] = now_iso()
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
    pu.set_defaults(fn=cmd_update)

    # move
    pm = sub.add_parser("move", help="change a card's column")
    pm.add_argument("ref")
    pm.add_argument("column")
    pm.add_argument("--writeup", default=None)
    pm.add_argument("--writeup-stdin", action="store_true")
    pm.set_defaults(fn=cmd_move)

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

    psim = sub.add_parser("sim", help="run canonical lifecycle: task → inprogress → done")
    psim.add_argument("--title", default=None, help="card title (default: auto-named)")
    psim.add_argument("--priority", default="mid", choices=["critical", "mid", "low"])
    psim.add_argument("--intervals", default="2,5",
                      help="seconds between phases: 'task→ip,ip→done' (default '2,5')")
    psim.add_argument("--writeup", default=None, help="custom done writeup (auto if omitted)")
    psim.set_defaults(fn=cmd_sim)

    pls = sub.add_parser("list", help="list cards (filtered)")
    pls.add_argument("--column", default=None)
    pls.add_argument("--priority", default=None)
    pls.add_argument("--tag", default=None)
    pls.set_defaults(fn=cmd_list)

    return ap


def main():
    args = build_parser().parse_args()
    board = find_board(args.board)
    d = load(board)
    args.fn(args, d, board)


if __name__ == "__main__":
    main()
