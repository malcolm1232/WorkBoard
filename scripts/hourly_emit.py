#!/usr/bin/env python3
"""hourly_extractor card emission + progress banner — extracted from hourly_extractor.py (#307).

How discovered cards and the progress banner get written to the board (all via
card.py subprocess). A pure leaf: nothing here calls back into the extractor,
reconciler, or digest builder, so both hourly_extractor and hourly_reconcile
import it freely.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _banner_update_text(card_py: Path, board: Path, num: int, title: str) -> None:
    args = [sys.executable, str(card_py), "--board", str(board), "update",
            str(num), "--title", title]
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=4)
    except subprocess.SubprocessError:
        pass


# ---------- progress banner ----------

def _banner_create(card_py: Path, board: Path, total_chunks: int) -> int | None:
    """Spawn the live progress banner in the 'notes' column."""
    args = [sys.executable, str(card_py), "--board", str(board), "add",
            "--column", "notes", "--priority", "mid",
            "--code", "EXTRACTION",
            "--title", f"🔄 extracting 0/{total_chunks} chunks…",
            "--origin", "Live progress banner from hourly_extractor.py",
            "--notes", f"chunks done: 0/{total_chunks}  cards emitted: 0",
            "--tag", "discovered", "--tag", "banner"]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=8)
    except subprocess.SubprocessError:
        return None
    if out.returncode != 0:
        return None
    m = re.search(r"#(\d+)", out.stdout)
    return int(m.group(1)) if m else None


def _banner_update(card_py: Path, board: Path, num: int,
                   done: int, total: int, cards_so_far: int) -> None:
    args = [sys.executable, str(card_py), "--board", str(board), "update",
            str(num),
            "--title", f"🔄 extracting {done}/{total} chunks…",
            "--notes", f"chunks done: {done}/{total}  cards emitted: {cards_so_far}"]
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=4)
    except subprocess.SubprocessError:
        pass


def _banner_finish(card_py: Path, board: Path, num: int,
                   n_cards: int, n_buckets: int, n_chunks: int,
                   n_moved: int = 0) -> None:
    recon_tag = f", reconciled {n_moved}" if n_moved else ""
    args = [sys.executable, str(card_py), "--board", str(board), "update",
            str(num),
            "--title", f"✓ extraction done — {n_cards} cards{recon_tag}",
            "--notes", f"emitted {n_cards} card(s) across {n_buckets} bucket(s) "
                       f"in {n_chunks} chunk(s). recon moved {n_moved} card(s)."]
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=4)
    except subprocess.SubprocessError:
        pass


def _card_add(card_py: Path, board: Path, card: dict) -> int | None:
    title = (card.get("title") or "").strip()[:80]
    if not title:
        return None
    code = (card.get("code") or "").strip()
    # The code renders as its own badge — keep the title CLEAN (no "CODE: " prefix),
    # matching the manual board (code 'BOARD-AUTO-MOVE' + title 'Auto-promotion …').
    # Strip a redundant leading "CODE:" if the LLM put one in the title.
    if code and title.lower().startswith(code.lower()):
        title = title[len(code):].lstrip(" :—-").strip() or title
    column = card.get("column") or "task"
    if column not in ("task", "backlog", "inprogress", "done",
                      "mandatory", "notes"):
        column = "task"
    priority = card.get("priority") or "mid"
    if priority not in ("low", "mid", "critical"):
        priority = "mid"
    notes = (card.get("notes") or "").strip()[:400]
    tags = card.get("tags") or []
    origin = card.get("origin") or f"Hourly extract — bucket {card.get('_bucket_label','')}"

    args = [sys.executable, str(card_py), "--board", str(board), "add",
            "--column", column, "--priority", priority,
            "--title", title, "--origin", origin[:400],
            "--tag", "discovered"]
    # Set the code FIELD (not just the title prefix) so the colored code badge
    # renders on the card — matching the manual board (e.g. SIM-60D, BOARD-SLIM).
    if code:
        args += ["--code", code[:24]]
    # Stamp createdAt with the bucket's actual time so the board sorts
    # chronologically without an end-pass.
    bucket_ts = card.get("_bucket_ts_iso")
    if bucket_ts:
        args += ["--created-at", bucket_ts]
    if notes:
        args += ["--notes", notes]
    for t in tags:
        if isinstance(t, str) and t.strip():
            args += ["--tag", t.strip()]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=8)
    except subprocess.SubprocessError:
        return None
    if out.returncode != 0:
        return None
    m = re.search(r"#(\d+)", out.stdout)
    return int(m.group(1)) if m else None


def _card_fly(card_py: Path, board: Path, num: int, col: str,
              writeup: str | None = None, bug: str | None = None,
              improve: str | None = None, subtask: str | None = None) -> bool:
    args = [sys.executable, str(card_py), "--board", str(board), "fly",
            str(num), col, "--pause-ms", "150"]
    if writeup:
        args += ["--writeup", writeup[:200]]
    if bug:
        args += ["--bug", bug[:120]]
    if improve:
        args += ["--improve", improve[:120]]
    if subtask:
        args += ["--subtask", subtask[:120]]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=8)
    except subprocess.SubprocessError:
        return False
    return out.returncode == 0


def _replay_transitions(card_py: Path, board: Path, num: int,
                         transitions, pace_s: float) -> int:
    """Replay the richer historical path (#294 SIM-RICH-LIFECYCLE) — extra hops
    AFTER the initial ship: a `bug` reopen flies done→IP with a 🐞 subtask; an
    `improve` reopen flies done→IP with an improvement subtask; a `done` hop
    closes the cycle. The card.py fly --bug/--improve flags do the tag+subtask
    bookkeeping; history[] (#258) records every hop. Returns hops replayed.
    Silently ignores malformed entries so a bad LLM field can't break the fill."""
    if not isinstance(transitions, list):
        return 0
    hops = 0
    for t in transitions:
        if not isinstance(t, dict):
            continue
        to = t.get("to")
        if to not in ("inprogress", "done"):
            continue
        kind = t.get("kind")
        reason = (t.get("reason") or "").strip()
        time.sleep(pace_s)
        if to == "inprogress" and kind == "bug":
            ok = _card_fly(card_py, board, num, "inprogress", bug=reason or "regression after ship")
        elif to == "inprogress" and kind == "improve":
            ok = _card_fly(card_py, board, num, "inprogress", improve=reason or "enhancement after ship")
        elif to == "inprogress":
            ok = _card_fly(card_py, board, num, "inprogress")
        else:  # done — closes the reopened cycle
            ok = _card_fly(card_py, board, num, "done", writeup=reason or "shipped (replay)")
        hops += 1 if ok else 0
    return hops


def emit_card(card_py: Path, board: Path, card: dict,
              show_lifecycle: bool, pace_s: float) -> int | None:
    """Add the card, then optionally walk lifecycle hops if show_lifecycle."""
    final_col = card.get("column") or "task"
    if show_lifecycle and final_col in ("done", "inprogress"):
        # Start in task → fly to final
        card_for_add = dict(card)
        card_for_add["column"] = "task"
        num = _card_add(card_py, board, card_for_add)
        if num is None:
            return None
        time.sleep(pace_s)
        if final_col == "done":
            _card_fly(card_py, board, num, "inprogress")
            time.sleep(pace_s)
            _card_fly(card_py, board, num, "done",
                      writeup=card.get("notes") or "shipped (replay)")
            # #294: reconstruct the true post-ship path (bug bounces / improves)
            _replay_transitions(card_py, board, num, card.get("transitions"), pace_s)
        else:  # inprogress
            _card_fly(card_py, board, num, "inprogress")
        return num
    else:
        return _card_add(card_py, board, card)



__all__ = [
    "_banner_update_text", "_banner_create", "_banner_update", "_banner_finish",
    "_card_add", "_card_fly", "_replay_transitions", "emit_card",
]
