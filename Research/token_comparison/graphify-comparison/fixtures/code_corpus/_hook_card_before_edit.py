#!/usr/bin/env python3
"""PreToolUse 'card-before-edit' WARN hook (#75, yesterday opt 3).

Enforces law #1 (declare work up front) SOFTLY. When Claude is about to
Edit/Write a file inside a project that HAS a board, but NO card is currently
in `inprogress` on that board, it injects a non-blocking reminder to declare
one (`card.py add` → `fly inprogress`) before editing.

It NEVER blocks the edit — it emits only
`hookSpecificOutput.additionalContext` (a mirror, not a gate), matching the
project decision to enforce live-carding by discipline + visibility, not a hard
PreToolUse wall (which would fire on every trivial edit and become noise). It is
deliberately CONSERVATIVE — silent when:
  - the edited file is in no board project (walk-up finds no board/board.json),
  - the target is the board's own state (board.json / index.json / *_state /
    recon_pending.json) — those are card.py's writes, not user work,
  - a card is already `inprogress` on that board (work IS declared),
  - it already warned on this board in the last DEBOUNCE_SEC (no per-edit spam).

Self-contained (no card.py / hourly imports). Reads the PreToolUse payload from
stdin, silent-fails, exits 0 always.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DEBOUNCE_SEC = 60          # at most one warn per board per minute
SKIP_NAMES = {"board.json", "index.json", "recon_pending.json"}


def find_board(start: Path) -> Path | None:
    """Walk up to 8 levels for board/board.json (same contract as the other hooks)."""
    cur = start
    for _ in range(8):
        cand = cur / "board" / "board.json"
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _is_board_state_file(fpath: str) -> bool:
    name = Path(fpath).name
    if name in SKIP_NAMES or name.endswith("_state.json"):
        return True
    # any .json sitting directly under a board/ data dir is steward state, not work
    parts = Path(fpath).parts
    return "board" in parts and name.endswith(".json")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool = payload.get("tool_name") or ""
    if tool not in EDIT_TOOLS:
        return 0

    ti = payload.get("tool_input") or {}
    fpath = ti.get("file_path") or ti.get("notebook_path") or ""
    cwd = payload.get("cwd") or ""

    # Anchor on the edited file's dir first (handles edits outside cwd), then cwd.
    anchor = Path(fpath).parent if fpath else (Path(cwd) if cwd else None)
    board_path = (find_board(anchor) if anchor else None) or (
        find_board(Path(cwd)) if cwd else None)
    if board_path is None:
        return 0                         # not a board project → silent

    if fpath and _is_board_state_file(fpath):
        return 0                         # editing steward state, not user work

    try:
        board = json.loads(board_path.read_text(errors="replace"))
    except Exception:
        return 0
    cards = board.get("cards") or []
    has_ip = any(c.get("column") == "inprogress" and not c.get("doneAt")
                 for c in cards)
    if has_ip:
        return 0                         # work already declared → silent

    # Debounce: one nudge per board per DEBOUNCE_SEC (don't spam a flurry of edits).
    state_path = board_path.parent / ".card_before_edit_state.json"
    now = time.time()
    try:
        last = json.loads(state_path.read_text()).get("at", 0)
    except Exception:
        last = 0
    if now - last < DEBOUNCE_SEC:
        return 0
    try:
        state_path.write_text(json.dumps({"at": now}))
    except OSError:
        pass

    fname = Path(fpath).name if fpath else "this file"
    msg = (
        f"⚠ board-steward (law #1 — declare up front): about to edit {fname} but "
        f"NO card is In-Progress on this board. If this edit is a unit of work the "
        f"user would reference later, declare it FIRST so it tracks live (not "
        f"batched): `card.py add --column task --title \"<verb+noun>\" --origin "
        f"\"<their words>\"` → `card.py fly <n> inprogress`. If it's a trivial / "
        f"read-only-adjacent / exploratory edit, ignore this — it does NOT block."
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": msg,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
