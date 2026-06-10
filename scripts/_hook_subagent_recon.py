#!/usr/bin/env python3
"""Subagent auto-card backstop — the "every task labelled regardless of source" guarantee.

Wired to TWO Claude Code hook events:
  spawn  <- PreToolUse(matcher 'Agent')  : a subagent is being launched
  stop   <- SubagentStop                 : a subagent just finished

PreToolUse(Agent) payload (stdin):
    {hook_event_name, tool_name:'Agent',
     tool_input:{description, subagent_type, prompt, ...}, cwd, ...}
SubagentStop payload (stdin):
    {session_id, transcript_path, cwd, stop_hook_active, ...}

Design (matches project_agent_to_agent_next.md):
  - Subagents have no UserPromptSubmit/Stop the board watches, so they never
    auto-card on their own. These two hooks close that gap WITHOUT the subagent
    needing to know the board protocol — the carding is done BY the hook,
    autonomously.
  - Full fly: spawn -> card born in 'task', flown to 'inprogress'; stop -> flown
    to 'done'. board.html animates each move live.
  - Read-only subagent types (Explore/Plan) are SKIPPED (no card) — but still
    pushed to the FIFO queue as a skip-marker so spawn<->stop stays aligned when
    read-only and working subagents interleave sequentially.
  - Correlation = a FIFO sidecar queue (board/.subagent_queue.jsonl). Correct
    for SEQUENTIAL subagents (the `claude -p` agent-to-agent loop, the headline
    case). Parallel / background subagents finish out of order -> a card still
    flies to done, but its writeup may pair with the wrong sibling. This is
    LOGGED in the writeup, never silently hidden.
  - At stop, the subagent's transcript is scanned for nested Agent spawns
    (surfaced as a subtask) and explicit bug markers (surfaced in the writeup).
    Bugs are NOT auto-tagged from soft heuristics — only flagged for review — so
    we never invent defect cards (no guessing).

Silent-fail, exit 0 always. Never blocks the subagent.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Subagent types that are pure read-only recon — not carded (user choice).
SKIP_TYPES = {"explore", "plan"}

QUEUE_NAME = ".subagent_queue.jsonl"
NUM_RE = re.compile(r"#(\d+)")
SID_RE = re.compile(r"subtask \+ (\S+):")   # parse new subtask id from card.py
# Conservative, explicit-only bug markers in the subagent's OWN output.
BUG_MARKERS = ("bug:", "regression:", "traceback (most recent call last)")
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit"}

# --- Subagent card-tracking MODE dial (#79) ---------------------------------
# Controls WHERE a subagent's work lands, so internal tooling agents don't
# pollute the board with top-level cards while genuine agent-to-agent product
# work still maps richly. Resolved per board: env override → board.settings →
# default. The three modes:
#   off      — no subagent tracking at all (total silence).
#   subtask  — (DEFAULT, = option 1c) the subagent's work becomes a SUBTASK of
#              the active In-Progress card; if there's no active card, NOTHING is
#              created (an internal helper with nothing to attach to is noise).
#   collab   — (= option 1b opt-in, for agent-to-agent product builds) the
#              subagent gets its OWN child card linked to an epic
#              (board.settings.subagentEpic), mirroring the agent tree.
VALID_MODES = {"off", "subtask", "collab"}
DEFAULT_MODE = "subtask"


def resolve_mode(board: Path) -> str:
    env = (os.environ.get("BOARD_SUBAGENT_CARDS") or "").strip().lower()
    if env in VALID_MODES:
        return env
    try:
        d = json.loads(board.read_text(errors="replace"))
        m = ((d.get("settings") or {}).get("subagentCards") or "").strip().lower()
        if m in VALID_MODES:
            return m
    except Exception:
        pass
    return DEFAULT_MODE


def _board_dict(board: Path) -> dict:
    try:
        return json.loads(board.read_text(errors="replace"))
    except Exception:
        return {}


def active_card_num(board: Path) -> str | None:
    """The card the work is currently flowing into = the active IP pulse.
    Prefer board.activeWorkId (a card id); fall back to the most-recently-updated
    inprogress card. None if nothing is in flight."""
    d = _board_dict(board)
    cards = d.get("cards") or []
    awid = d.get("activeWorkId")
    if awid:
        for c in cards:
            if c.get("id") == awid:
                return str(c.get("num"))
    ip = [c for c in cards
          if c.get("column") == "inprogress" and not c.get("doneAt")]
    if not ip:
        return None
    ip.sort(key=lambda c: c.get("updatedAt") or "", reverse=True)
    return str(ip[0].get("num"))


def epic_num(board: Path) -> str | None:
    """The collab-mode epic card the orchestrator stashed (settings.subagentEpic)."""
    n = (_board_dict(board).get("settings") or {}).get("subagentEpic")
    return str(n) if n else None


def find_board(start: Path) -> Path | None:
    cur = start.resolve()
    for _ in range(8):
        cand = cur / "board" / "board.json"
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def run_card(board: Path, args: list[str]) -> str:
    """Invoke card.py (sibling of this file) against an explicit board. Best-effort."""
    card_py = Path(__file__).resolve().parent / "card.py"
    try:
        cp = subprocess.run(
            [sys.executable, str(card_py), "--board", str(board), *args],
            capture_output=True, text=True, timeout=5,
        )
        return (cp.stdout or "") + (cp.stderr or "")
    except Exception:
        return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def queue_path(board: Path) -> Path:
    return board.parent / QUEUE_NAME


def queue_push(board: Path, entry: dict) -> None:
    try:
        with queue_path(board).open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def queue_pop(board: Path) -> dict | None:
    """Pop the oldest entry (FIFO). Rewrites the file without its first line."""
    qp = queue_path(board)
    try:
        lines = [ln for ln in qp.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    first, rest = lines[0], lines[1:]
    try:
        qp.write_text(("\n".join(rest) + "\n") if rest else "")
    except OSError:
        pass
    try:
        return json.loads(first)
    except Exception:
        return None


# ---- #566 subtask 2: content-correlated pairing ----------------------------
# Blind FIFO pop pairs a SubagentStop with the OLDEST spawn entry. Correct for
# SEQUENTIAL subagents, but TRULY PARALLEL subagents finish out of order, so the
# oldest entry is the wrong sibling → the writeup lands on the wrong card. Fix:
# correlate a stop to ITS spawn by the subagent's task-prompt signature (the
# subagent's own first user message == the prompt it was spawned with), and pop
# THAT entry. Falls back to oldest (legacy FIFO) when nothing correlates, so the
# sequential case and any correlation-miss are never worse than before.
_CORR_MIN = 30          # min signature overlap (chars) to trust a correlation


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _first_user_prompt(path: Path) -> str:
    """The text of the FIRST genuine user message in a (subagent) transcript —
    i.e. the task prompt it was spawned with. Skips tool_result 'user' lines."""
    try:
        with path.open("r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "user":
                    continue
                msg = o.get("message") or {}
                c = msg.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    has_tr = any(isinstance(b, dict) and b.get("type") == "tool_result"
                                 for b in c)
                    txt = " ".join(b.get("text", "") for b in c
                                   if isinstance(b, dict) and b.get("type") == "text")
                    if txt and not has_tr:
                        return txt
    except OSError:
        pass
    return ""


def _corr_score(psig: str, tsig: str) -> int:
    """Overlap between a spawn signature and a stop signature. Containment (one is
    a substring of the other — handles the spawn's truncation / any harness
    preamble) scores the shorter length; else the common-prefix length."""
    if not psig or not tsig:
        return 0
    if psig in tsig or tsig in psig:
        return min(len(psig), len(tsig))
    n = 0
    for x, y in zip(psig, tsig):
        if x != y:
            break
        n += 1
    return n


def queue_pop_correlated(board: Path, tsig: str) -> dict | None:
    """Pop the queue entry whose stored psig best correlates with `tsig` (the
    finished subagent's first-prompt signature). Falls back to oldest (FIFO) when
    nothing clears _CORR_MIN — preserving legacy behaviour for sequential runs."""
    qp = queue_path(board)
    try:
        lines = [ln for ln in qp.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    entries = []
    for ln in lines:
        try:
            entries.append(json.loads(ln))
        except Exception:
            entries.append(None)
    idx, best = None, 0
    if tsig:
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                continue
            score = _corr_score(e.get("psig") or "", tsig)
            if score >= _CORR_MIN and score > best:
                best, idx = score, i
    if idx is None:
        idx = 0                                   # FIFO fallback (oldest)
    popped = entries[idx]
    rest = [lines[i] for i in range(len(lines)) if i != idx]
    try:
        qp.write_text(("\n".join(rest) + "\n") if rest else "")
    except OSError:
        pass
    return popped if isinstance(popped, dict) else None


# ---- spawn (PreToolUse 'Agent') --------------------------------------------

def do_spawn(payload: dict) -> None:
    if (payload.get("tool_name") or "").lower() != "agent":
        return
    ti = payload.get("tool_input") or {}
    desc = (ti.get("description") or "subagent task").strip()[:80]
    stype = (ti.get("subagent_type") or "subagent").strip()
    prompt = (ti.get("prompt") or "").strip().replace("\n", " ")[:200]
    psig = _norm(prompt)[:160]                # #566 subtask 2 — correlation key
    cwd = payload.get("cwd") or ""
    if not cwd:
        return
    board = find_board(Path(cwd))
    if board is None:
        return

    mode = resolve_mode(board)
    if mode == "off":
        return                            # no tracking; stop is also a no-op

    # Read-only recon -> don't card, but keep FIFO aligned with a skip marker.
    if stype.lower() in SKIP_TYPES:
        queue_push(board, {"skip": True, "type": stype, "desc": desc,
                           "ts": _now(), "psig": psig})
        return

    if mode == "subtask":
        # (1c) Attach to the active In-Progress card as a subtask. No active card
        # => nothing to attach to => create NOTHING (internal helper = noise).
        parent = active_card_num(board)
        if parent is None:
            queue_push(board, {"skip": True, "type": stype, "desc": desc,
                               "ts": _now(), "note": "no-active-card", "psig": psig})
            return
        out = run_card(board, ["subtask", "add", parent,
                               f"[subagent:{stype}] {desc}"])
        ms = SID_RE.search(out)
        if not ms:
            queue_push(board, {"skip": True, "type": stype, "desc": desc,
                               "ts": _now(), "note": "subtask-add-failed", "psig": psig})
            return
        queue_push(board, {"parent": parent, "sid": ms.group(1),
                           "type": stype, "desc": desc, "ts": _now(), "psig": psig})
        return

    # mode == "collab": own child card, linked to the epic if one is set.
    add_args = ["add", "--column", "task", "--title", f"{desc}",
                "--tag", "subagent", "--tag", stype,
                "--origin", f"[subagent:{stype}] {prompt}"]
    epic = epic_num(board)
    if epic:
        add_args += ["--link", epic]
    out = run_card(board, add_args)
    m = NUM_RE.search(out)
    if not m:
        # Card add failed — record a skip marker so the matching stop is a no-op
        # rather than popping (and flying) some unrelated card.
        queue_push(board, {"skip": True, "type": stype, "desc": desc,
                           "ts": _now(), "note": "add-failed", "psig": psig})
        return
    num = m.group(1)
    run_card(board, ["fly", num, "inprogress", "--pause-ms", "120"])
    queue_push(board, {"card": num, "type": stype, "desc": desc,
                       "ts": _now(), "epic": epic, "psig": psig})


# ---- stop (SubagentStop) ---------------------------------------------------

def scan_subagent_transcript(path: Path) -> dict:
    """Tally a finished subagent's transcript: nested spawns, edits, bug markers."""
    nested, edits, bug = [], 0, False
    last_text = ""
    try:
        with path.open("r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "assistant":
                    continue
                msg = o.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "tool_use":
                        nm = str(blk.get("name", "")).lower()
                        if nm == "agent":
                            inp = blk.get("input") or {}
                            d = (inp.get("description") or "").strip()[:60]
                            if d:
                                nested.append(d)
                        elif nm in EDIT_TOOLS:
                            edits += 1
                    elif blk.get("type") == "text":
                        last_text = str(blk.get("text", ""))
    except OSError:
        pass
    low = last_text.lower()
    if any(mk in low for mk in BUG_MARKERS):
        bug = True
    return {"nested": nested, "edits": edits, "bug": bug}


def do_stop(payload: dict) -> None:
    if payload.get("stop_hook_active"):
        return
    cwd = payload.get("cwd") or ""
    if not cwd:
        return
    board = find_board(Path(cwd))
    if board is None:
        return
    if resolve_mode(board) == "off":
        return                            # spawn pushed nothing; nothing to pop
    # #566 subtask 2 — correlate this stop to ITS spawn by task-prompt signature
    # (the subagent's own first user message), so parallel out-of-order finishes
    # pair their writeup to the right card instead of by blind FIFO position.
    transcript = payload.get("transcript_path") or ""
    tsig = _norm(_first_user_prompt(Path(transcript)))[:200] if transcript else ""
    entry = queue_pop_correlated(board, tsig)
    if entry is None or entry.get("skip"):
        # No working card to close (read-only subagent, add-failure, no active
        # card in subtask mode, or an orphaned stop). Silent — never invent one.
        return

    # subtask mode (1c): just close the subtask under its parent card. No
    # writeup card-fly; the subtask checkbox is the live signal.
    if entry.get("sid"):
        run_card(board, ["subtask", "done", entry["parent"], entry["sid"]])
        return

    num = entry.get("card")
    if not num:
        return

    act = (scan_subagent_transcript(Path(transcript))
           if transcript else {"nested": [], "edits": 0, "bug": False})

    stype = entry.get("type", "subagent")
    desc = entry.get("desc", "")
    parts = [f"Subagent ({stype}) completed: {desc}."]
    if act["edits"]:
        parts.append(f"Made {act['edits']} file edit(s).")
    if act["nested"]:
        parts.append("Spawned nested subagent(s): " + "; ".join(act["nested"][:6]) + ".")
    if act["bug"]:
        parts.append("WARN output contained an explicit bug/traceback marker — "
                     "review and file a bug card if real.")
    parts.append("(Auto-carded by SubagentStop hook; paired to its spawn by "
                 "task-prompt signature — parallel-safe, FIFO only as fallback.)")
    writeup = " ".join(parts)

    fly_args = ["fly", num, "done", "--writeup", writeup, "--pause-ms", "120"]
    if act["nested"]:
        fly_args[5:5] = ["--subtask",
                         f"{len(act['nested'])} nested subagent(s)"]
    run_card(board, fly_args)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        if mode == "spawn":
            do_spawn(payload)
        elif mode == "stop":
            do_stop(payload)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
