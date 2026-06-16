#!/usr/bin/env python3
"""hourly_extractor card emission + progress banner — extracted from hourly_extractor.py (#307).

How discovered cards and the progress banner get written to the board (all via
card.py subprocess). A pure leaf: nothing here calls back into the extractor,
reconciler, or digest builder, so both hourly_extractor and hourly_reconcile
import it freely.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# #574: a done card must visibly pass through inprogress. At speedup pacing
# pace_s≈0, so floor the IP dwell to this many seconds so the hop is perceptible
# (the card reads as task→IP→done, not "straight to done").
_MIN_IP_DWELL_S = 0.35


def _banner_update_text(card_py: Path, board: Path, num: int, title: str) -> None:
    args = [sys.executable, str(card_py), "--board", str(board), "update",
            str(num), "--title", title]
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=4)
    except subprocess.SubprocessError:
        pass


# ---------- progress banner ----------

def _emit_progress(card_py: Path, board: Path, done: int, total: int,
                   label: str = "", phase: str = "", final: bool = False) -> None:
    """#318 — drive the live BOARD-LOAD HUD via `card.py progress` (best-effort).
    phase (#327) sets the HUD header: replay / speedup / solo / reconcile / inline('').
    final (#327 single-HUD) marks the LAST emit of the whole fill — only then does
    the HUD complete (✓) and auto-hide; intermediate stage-ends hand off instead."""
    try:
        args = [sys.executable, str(card_py), "--board", str(board), "progress",
                "--done", str(done), "--total", str(total), "--label", label,
                "--phase", phase]
        if final:
            args.append("--final")
        subprocess.run(args, capture_output=True, text=True, timeout=4)
    except subprocess.SubprocessError:
        pass


# ---------- progress heartbeat (#638) ----------

class _HudPulse:
    """Liveness tracker for progress_heartbeat. The work thread calls .touch()
    after each REAL HUD emit; the heartbeat thread fires a 'still working…' tick
    only when nothing has touched it for `interval` — so it fills stalls without
    flickering over normal per-chunk progress."""
    def __init__(self) -> None:
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def touch(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def quiet_for(self) -> float:
        with self._lock:
            return time.monotonic() - self._last


@contextmanager
def progress_heartbeat(card_py: Path, board: Path, status,
                       phase: str = "", interval: float = 5.0):
    """#638 — keep the BOARD-LOAD HUD alive during a slow/silent stage so it never
    freezes on a stale number (looks-hung → user force-quits mid-bootstrap). While
    the `with` block runs, a daemon thread emits a 'still working… mm:ss' progress
    tick whenever the stage has been quiet (no real emit) for `interval` seconds.

    The two freeze zones this covers (#638): a slow/retrying Haiku chunk in
    _extract_haiku, and the 60-90s end-of-replay reconcile (one silent _llm_reconcile
    call). `status` is Callable[[], (done, total, base_label)] read live so the tick
    reflects current counts. Yields the _HudPulse — call .touch() after your own
    real emits so the heartbeat backs off and fires only during genuine stalls.
    Best-effort: a heartbeat failure never disturbs the work."""
    pulse = _HudPulse()
    stop = threading.Event()
    t0 = time.monotonic()
    poll_s = min(1.0, max(0.05, interval))   # detect a stall within ~one interval

    # Enclosing names are bound as defaults (not closed over) so the static
    # name-audit stays strict — same convention as _extract_haiku._emit_chunk_cards.
    def _run(card_py=card_py, board=board, status=status, phase=phase,
             interval=interval, poll_s=poll_s, t0=t0, pulse=pulse,
             stop=stop) -> None:
        # Poll periodically; only emit when the stage has actually stalled.
        while not stop.wait(poll_s):
            if pulse.quiet_for() < interval:
                continue
            try:
                done, total, base = status()
                mm, ss = divmod(int(time.monotonic() - t0), 60)
                tail = f"{base} · still working… {mm}:{ss:02d}" if base \
                    else f"still working… {mm}:{ss:02d}"
                _emit_progress(card_py, board, done, total, tail, phase)
            except Exception:
                pass
            pulse.touch()   # space ticks by `interval`; don't busy-emit

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    try:
        yield pulse
    finally:
        stop.set()
        th.join(timeout=2.0)


def _banner_create(card_py: Path, board: Path, total_chunks: int,
                   phase: str = "") -> int | None:
    """Kick off extraction progress on the live BOARD-LOAD HUD.

    The HUD (#318) is the single source of truth for "X/Y chunks". The old
    'notes'-column banner card was redundant with it (user, 2026-06-01), so it's
    gone — we only drive the HUD here and return None (no banner card to update).
    """
    _emit_progress(card_py, board, 0, total_chunks,
                   "staged — beginning extraction…", phase)
    return None


def _banner_update(card_py: Path, board: Path, num: int,
                   done: int, total: int, cards_so_far: int,
                   phase: str = "", label_override: str | None = None,
                   final: bool = False) -> None:
    # The notes-column banner card is gone (num is None) — progress lives only
    # on the HUD now. #327 — label_override lets the tier-1→tier-2 handoff
    # replace the generic "chunk N/M" line with e.g. "day-1 replayed in 8s ▸▸".
    # final=True only when this extraction stage is the LAST thing in the fill
    # (no reconcile sweep to follow) — then the HUD completes here.
    # The headline "N/M" now owns the chunk counter (#327 single-HUD, 1-based) —
    # so the tail line no longer repeats a (differently-based) "chunk N/M"; it
    # carries the complementary detail instead (cards emitted so far).
    _emit_progress(card_py, board, done, total,
                   label_override or f"{cards_so_far} card(s) emitted so far",
                   phase, final=final)


def _banner_finish(card_py: Path, board: Path, num: int,
                   n_cards: int, n_buckets: int, n_chunks: int,
                   n_moved: int = 0) -> None:
    # The notes-column banner card is gone — nothing to finalize. The HUD's
    # final state is driven by the last _banner_update / the HUD's own done
    # handling. Kept as a no-op so callers don't need to change.
    return None


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
                      "super-urgent", "notes"):
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


def _card_subtask_add(card_py: Path, board: Path, num: int, text: str,
                      parent: str | None = None) -> str | None:
    """Add ONE subtask via the card.py CLI (#570 — bootstrap decomposition,
    same path live carding uses). Returns the new subtask id (parsed from the
    command's '+ s-…:' line) so the caller can tick it done, or None on
    failure. Silently tolerant — a bad subtask must never break the fill."""
    text = (text or "").strip()
    if not text:
        return None
    args = [sys.executable, str(card_py), "--board", str(board),
            "subtask", "add", str(num), text[:160]]
    if parent:
        args += ["--parent", parent]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=8)
    except subprocess.SubprocessError:
        return None
    if out.returncode != 0:
        return None
    m = re.search(r"\+\s+(s-[a-z0-9-]+)", out.stdout)
    return m.group(1) if m else None


def _emit_subtasks(card_py: Path, board: Path, num: int, subtasks,
                   mark_done: bool) -> int:
    """Decompose a multi-part mined card into REAL subtasks (#570 — transpose
    live shape into bootstrap), so the card matches a live-carded one instead of
    the auto 1/1 'initial ship'. SHAPE-NEUTRAL: an empty/missing list is a no-op
    (single-part cards keep today's behavior). Each item is a plain string, or a
    {"text", "children":[…]} dict (one level of nesting for the grouped case).
    When mark_done, ticks every emitted subtask so a shipped card reads N/N.
    Returns the count of top-level subtasks emitted."""
    if not isinstance(subtasks, list) or not subtasks:
        return 0
    n = 0
    for item in subtasks[:4]:                     # ≤4 flat segments (SKILL 2a)
        if isinstance(item, dict):
            text = item.get("text") or ""
            children = item.get("children") or []
        else:
            text, children = item, []
        sid = _card_subtask_add(card_py, board, num, text)
        if not sid:
            continue
        n += 1
        for child in (children or [])[:4]:
            ctext = child.get("text") if isinstance(child, dict) else child
            csid = _card_subtask_add(card_py, board, num, ctext, parent=sid)
            if csid and mark_done:
                _card_subtask_done(card_py, board, num, csid)
        if mark_done:
            _card_subtask_done(card_py, board, num, sid)
    return n


def _card_subtask_done(card_py: Path, board: Path, num: int, sid: str) -> bool:
    try:
        out = subprocess.run(
            [sys.executable, str(card_py), "--board", str(board),
             "subtask", "done", str(num), sid],
            capture_output=True, text=True, timeout=8)
    except subprocess.SubprocessError:
        return False
    return out.returncode == 0


def _card_review(card_py: Path, board: Path, num: int, skill: str,
                 at: str | None = None) -> bool:
    """Stamp a mined card as code-reviewed (#599 backfill) via the SHIPPED #598
    `card.py review` mechanism — reviewed tag + reviewedAt + 🔍 subtask. The
    review skill was detected in the very turns that became this card, so this is
    attribution, not fuzzy matching. --sha "" is honest: the bootstrap HEAD is not
    the reviewed sha. Best-effort — a bad stamp must never break the fill."""
    skill = (skill or "").split(":")[-1].strip()
    if not skill:
        return False
    args = [sys.executable, str(card_py), "--board", str(board), "review",
            str(num), "--skill", skill, "--sha", "", "--findings", "[bootstrap]"]
    if at:
        args += ["--at", at]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=8)
    except subprocess.SubprocessError:
        return False
    return out.returncode == 0


def _maybe_review(card_py: Path, board: Path, num: int, card: dict) -> None:
    """If the mined card carries a `reviewed` attribution, stamp it (#599)."""
    rv = card.get("reviewed")
    skill = rv.get("skill") if isinstance(rv, dict) else None
    if skill:
        _card_review(card_py, board, num, skill, card.get("_bucket_ts_iso"))


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
    # #575: bootstrap replay is AUTOMATION — bypass the live-carding guards
    # (#103 decompose-before-IP, #537 one-in-flight, #476 done-completeness).
    # Without this, a mined card with a ` + ` glance-title but an empty subtasks
    # array (common from Haiku) gets its fly→inprogress BLOCKED by #103; the
    # failure is swallowed and the next fly→done lands it task→done with NO IP
    # hop. The guards are the documented BOARD_SKIP_DECOMPOSE_CHECK bypass case.
    env = {**os.environ, "BOARD_SKIP_DECOMPOSE_CHECK": "1"}
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=8, env=env)
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
    subtasks = card.get("subtasks")
    # #574: replay a lifecycle for done/inprogress AND backlog — a backlog card
    # is work that was STARTED then deferred, so it should glide task→backlog,
    # not just appear in backlog. (task/notes/super-urgent are born in place.)
    if show_lifecycle and final_col in ("done", "inprogress", "backlog"):
        # Start in task → decompose → fly to final
        card_for_add = dict(card)
        card_for_add["column"] = "task"
        num = _card_add(card_py, board, card_for_add)
        if num is None:
            return None
        # #570: emit REAL subtasks while still in task (before the fly) so the
        # card arrives shaped like a live one and never trips the #103
        # decompose-before-IP guard. A done card's parts are complete → tick
        # them (reads N/N); inprogress/backlog parts stay open.
        _emit_subtasks(card_py, board, num, subtasks,
                       mark_done=(final_col == "done"))
        time.sleep(pace_s)
        if final_col == "done":
            # #574: a done card MUST pass through inprogress, with a visible
            # dwell — at speedup pacing pace_s≈0 made the IP hop flash by, so it
            # read as "straight to done". Floor the IP dwell so the hop is seen.
            _card_fly(card_py, board, num, "inprogress")
            time.sleep(max(pace_s, _MIN_IP_DWELL_S))
            _card_fly(card_py, board, num, "done",
                      writeup=card.get("notes") or "shipped (replay)")
            # #294: reconstruct the true post-ship path (bug bounces / improves)
            _replay_transitions(card_py, board, num, card.get("transitions"), pace_s)
        elif final_col == "backlog":
            # deferred work — glide task→backlog (not a bare appearance in backlog)
            _card_fly(card_py, board, num, "backlog")
        else:  # inprogress
            _card_fly(card_py, board, num, "inprogress")
        _maybe_review(card_py, board, num, card)   # #599 review-coverage backfill
        return num
    else:
        num = _card_add(card_py, board, card)
        if num is None:
            return None
        # Non-lifecycle add (card born directly in its final column). Decompose
        # the same way; tick done only when it's a done card.
        _emit_subtasks(card_py, board, num, subtasks,
                       mark_done=(final_col == "done"))
        _maybe_review(card_py, board, num, card)   # #599 review-coverage backfill
        return num



__all__ = [
    "_banner_update_text", "_banner_create", "_banner_update", "_banner_finish",
    "_emit_progress", "progress_heartbeat", "_HudPulse",
    "_card_add", "_card_fly", "_card_review", "_maybe_review",
    "_replay_transitions", "emit_card",
]
