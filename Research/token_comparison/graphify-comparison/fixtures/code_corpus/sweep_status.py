#!/usr/bin/env python3
"""Completion guard for inline extraction (#315) — the forgotten-sweep klaxon.

A History Replay / bootstrap install STAGES the bucketed digest into
``board/extraction_pending.json`` and the protocol says: *"DELETE this file
when all chunks + the completeness sweep are done."* So the file is a
before/after marker — present BEFORE the sweep, gone AFTER. A leftover file
means the completeness sweep was skipped and never-miss cards may have been
silently dropped (the #314/#315 gap).

This module is the single source of truth for detecting that state. It is
deliberately pure-stdlib and dependency-free so BOTH the session-start hook
(which must stay self-contained and <100ms) and ``card.py sweep-status`` can
call it without importing the heavy card_state/card_commands chain. The
existence of the file is the signal — a corrupt/unparseable pending file still
counts as "sweep not done" (chunks reported as unknown), never as clean.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PENDING_NAME = "extraction_pending.json"


def pending_path(board: str | Path) -> Path:
    """Resolve the pending file for a board. Accepts the board.json path or its
    parent board/ dir (the hook passes the former)."""
    p = Path(board)
    base = p.parent if p.name.endswith(".json") else p
    return base / PENDING_NAME


def _fmt_age(written_at: str | None) -> str | None:
    if not written_at:
        return None
    try:
        when = datetime.fromisoformat(written_at.replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - when).total_seconds()
        if secs < 60:  # also covers small clock skew / future stamps
            return "just now"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
        return f"{int(secs // 86400)}d ago"
    except Exception:
        return written_at[:10]


def status(board: str | Path) -> dict:
    """Deterministic state of the completeness sweep for one board.

    Returns {pending, chunks, written_at, age, path}. ``pending`` is True iff a
    leftover extraction_pending.json exists (= sweep not done). ``chunks`` is
    the staged-chunk count, or None if the file is present but unparseable."""
    fp = pending_path(board)
    if not fp.exists():
        return {"pending": False, "chunks": 0, "written_at": None,
                "age": None, "path": str(fp)}
    chunks: int | None = None
    written_at: str | None = None
    try:
        payload = json.loads(fp.read_text())
        chunks = len(payload.get("chunks", []))
        written_at = payload.get("written_at")
    except Exception:
        pass  # file exists but is corrupt — still "sweep not done"
    return {"pending": True, "chunks": chunks, "written_at": written_at,
            "age": _fmt_age(written_at), "path": str(fp)}


def hook_line(board: str | Path) -> str:
    """One-line klaxon for the session-start digest. Empty string if clean
    (so the hook can drop it). Names the SWEEP explicitly so emit-then-delete
    can't quietly skip recon."""
    st = status(board)
    if not st["pending"]:
        return ""
    n = st["chunks"]
    n_txt = (f"{n} chunk(s) staged" if n is not None
             else "chunks staged (file unreadable)")
    age = f" ({st['age']})" if st["age"] else ""
    return (f"⚠️ INSTALL INCOMPLETE — {n_txt}{age} but the COMPLETENESS SWEEP is "
            f"NOT run. Emit cards from each chunk's digest, run the sweep (the "
            f"never-miss re-scan for mandatory/notes/deferrals), THEN delete "
            f"{st['path']}. See SKILL.md §J.")


def human(board: str | Path) -> tuple[str, int]:
    """Human-readable status + exit code (1 if sweep not done, else 0)."""
    st = status(board)
    if not st["pending"]:
        return ("✓ sweep complete — no extraction_pending.json staged", 0)
    n = st["chunks"]
    n_txt = f"{n} chunk(s) staged" if n is not None else "file present but UNREADABLE"
    age = f", written {st['age']}" if st["age"] else ""
    return ("\n".join((
        "⚠️  SWEEP NOT DONE — extraction_pending.json",
        f"    {n_txt}{age}",
        f"    {st['path']}",
        "    → emit cards per chunk, run the COMPLETENESS SWEEP (never-miss "
        "re-scan), then delete the file (SKILL.md §J)",
    )), 1)


def _main(argv: list[str]) -> int:
    board = None
    hook = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--board":
            i += 1
            board = argv[i] if i < len(argv) else None
        elif a == "--hook-line":
            hook = True
        i += 1
    if not board:
        print("usage: sweep_status.py --board <board.json> [--hook-line]",
              file=sys.stderr)
        return 2
    if hook:
        line = hook_line(board)
        if line:
            print(line)
        return 0  # hook is non-blocking; rc carries no meaning here
    text, rc = human(board)
    print(text)
    return rc


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
