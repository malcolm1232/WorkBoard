#!/usr/bin/env python3
"""Stop-hook session-end reconciliation backstop (#279).

The live "never-miss on sign-off" guarantee. When the agent ends a session,
nothing else verifies that every substantive work-unit got carded or that
shipped In-Progress cards moved to Done — that was only ever advisory prose in
SKILL.md §F. This helper closes the gap.

Reads the Claude Code Stop-hook payload from stdin:
    {session_id, transcript_path, cwd, stop_hook_active, ...}

It is SELF-CONTAINED (no discover2/hourly_extractor imports) and reads ONLY the
session's own transcript (transcript_path) — so it's fast and can't hang on a
580-file glob. It detects:
  1. UNCARDED WORK — the session shows ship signals (git commit / "shipped" /
     "deployed") or many file edits, but no card.py add/move/fly ran this
     session → work likely went un-tracked.
  2. OPEN IN-PROGRESS — cards still in inprogress at sign-off (gentle reminder
     to confirm done or leave a note).
  3. BATCHED-NOT-LIVE (#74) — cards that reached Done in this window with NO
     real in-flight dwell: born in Task but jumped straight to Done (never
     inprogress) or sat <BATCH_DWELL_SEC in inprogress. This is the
     add→done collapse the rev/marker checks are structurally blind to (a
     batched card still advances rev and runs card.py). NON-BLOCKING — it
     just surfaces the smell so live-carding self-corrects (VISION law #3).

When something is found it writes board/recon_pending.json (the existing schema,
tagged source=stop_recon) so the NEXT SessionStart surfaces it and the next main
Claude reconciles with full context. Non-blocking, silent-fail, exit 0 always.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
try:
    import need_detect  # noqa: E402  (#562 shared multi-need heuristic)
except Exception:       # never let a missing helper break the Stop hook
    need_detect = None

EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
EDIT_THRESHOLD = 3          # this many edits w/ no card = uncarded-work signal
CARD_MARKERS =("card.py add", "card.py move", "card.py fly", "card.py improve",
                "card.py bug", "card.py auto-ship", "card.py subtask")
# #591-A: in-repo edits to these basename patterns are session/log/scratch
# artifacts, not project work — they must not trip the un-carded backstop even
# though they live under project_root (the dump/log-write false positive).
LOG_SCRATCH_RE = re.compile(
    r"(conversation_verbatim_|conversation_raw_|session_log)", re.I)
# #591-D: a read-only git/inspection command that merely MENTIONS a ship word
# (e.g. `git branch --merged`, `git log --grep=shipped`) is NOT a ship. And a
# card.py command whose --writeup quotes "shipped"/"deployed" is the carding
# action itself — counting it as an un-carded ship-signal is doubly wrong.
SHIP_EXCLUDE_PREFIXES = (
    "git log", "git show", "git diff", "git status", "git branch",
    "git fetch", "git remote", "git rev-parse", "git describe", "git blame",
    "git stash list", "grep", "rg", "cat", "head", "tail", "curl", "echo", "ls")
BATCH_DWELL_SEC = 30        # <this in inprogress = no real in-flight time (#74)


def find_board(start: Path) -> Path | None:
    """Walk up to 8 levels for board/board.json."""
    cur = start.resolve()
    for _ in range(8):
        cand = cur / "board" / "board.json"
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _tool_name(o: dict) -> str:
    """Extract a tool name from an assistant message's content blocks."""
    msg = o.get("message") or {}
    content = msg.get("content")
    names = []
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "tool_use":
                names.append(str(blk.get("name", "")).lower())
    return " ".join(names)


def _bash_cmd(o: dict) -> str:
    """Pull Bash command strings from tool_use blocks (lowercased)."""
    msg = o.get("message") or {}
    content = msg.get("content")
    cmds = []
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "tool_use":
                inp = blk.get("input") or {}
                c = inp.get("command")
                if isinstance(c, str):
                    cmds.append(c)
    return "\n".join(cmds)


def _in_scope_edits(o: dict, project_root) -> int:
    """Count edit/write tool_use blocks in this assistant message whose target
    file is INSIDE project_root (#78). Edits to files OUTSIDE the board's
    project — e.g. ~/.claude memory files, another repo, scratch notes — are not
    board work, so they must not push a turn over the un-carded threshold (the
    false-positive that blocked turns doing only memory/doc edits). When
    project_root is None, or a path can't be determined, the edit counts
    (conservative — never under-count real project work)."""
    msg = o.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    n = 0
    for blk in content:
        if not (isinstance(blk, dict) and blk.get("type") == "tool_use"):
            continue
        if str(blk.get("name", "")).lower() not in EDIT_TOOLS:
            continue
        if project_root is None:
            n += 1
            continue
        inp = blk.get("input") or {}
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        if not fp:
            n += 1                       # unknown path → count (conservative)
            continue
        # #591-A: session/log/scratch artifacts (verbatim dumps, *.log, session
        # logs) are not project work even when they live under project_root.
        base = Path(fp).name
        if LOG_SCRATCH_RE.search(base) or base.endswith(".log"):
            continue
        try:
            rp = Path(fp).resolve()
            in_scope = rp == project_root or project_root in rp.parents
        except Exception:
            in_scope = True              # unparseable → count (conservative)
        if in_scope:
            n += 1
    return n


def _is_ship_command(cmd: str) -> bool:
    """#591-D: True only for an actual mutating ship (commit/push/merge/deploy),
    not a read-only command that merely MENTIONS a ship word. Conservative —
    genuine ships still match; reads and card.py writeups never do. Evaluated
    per pipeline/sequence segment so `git log --grep=x && git push` still counts
    the push."""
    for seg in re.split(r"[;&|]{1,2}", cmd.lower()):
        seg = seg.strip()
        if not seg:
            continue
        # a card.py board action (its --writeup may quote "shipped") is the
        # carding action itself — never a ship-signal.
        if "card.py" in seg:
            continue
        if seg.startswith(SHIP_EXCLUDE_PREFIXES):
            continue
        if "--grep" in seg or "--merged" in seg or "--no-merged" in seg:
            continue
        if (re.search(r"\bgit\s+commit\b", seg)
                or re.search(r"\bgit\s+push\b", seg)
                or re.search(r"\bgit\s+merge\b", seg)
                or "shipped" in seg or "deployed" in seg):
            return True
    return False


def _is_real_user(o: dict) -> bool:
    """True for a genuine user PROMPT, not a tool_result. Claude Code records
    tool results ALSO as type=='user', so naively resetting the per-turn window
    on every type=='user' line would reset mid-turn on every tool call. A real
    prompt's content is a plain string, or a list with a text block and NO
    tool_result block."""
    msg = o.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        has_text = any(isinstance(b, dict) and b.get("type") == "text"
                       for b in content)
        has_tool_result = any(isinstance(b, dict)
                              and b.get("type") == "tool_result"
                              for b in content)
        return has_text and not has_tool_result
    return False


def _user_text(o: dict) -> str:
    """Plain text of a real user prompt (str content, or concatenated text
    blocks). Used to measure how many needs a prompt named (#562)."""
    msg = o.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def scan_transcript(path: Path, project_root=None) -> dict:
    """Tally this session's activity from its own transcript jsonl. Edits are
    scoped to project_root (#78): edits to files outside the board's project
    don't count toward the un-carded-work threshold."""
    pr = None
    if project_root is not None:
        try:
            pr = Path(project_root).resolve()
        except Exception:
            pr = None
    edits = 0
    ship_signals = 0
    card_actions = 0
    user_turns = 0
    last_user = ""
    # #562 sign-off mirror — session totals (NOT reset per turn):
    #   max_needs    = the most needs any single user prompt named
    #   capture_units = cards + subtasks created this session. SHAPE-NEUTRAL:
    #     a 1-card+N-subtask capture and an N-card capture clear the same bar,
    #     so the mirror never pushes toward multi-card; it flags only a real DROP.
    max_needs = 1
    capture_units = 0
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
                tp = o.get("type")
                if tp == "user":
                    # Per-turn windowing (#385 Flaw 1): a real user prompt is a
                    # NEW turn boundary, so reset the tallies — the backstop must
                    # judge THIS turn's work, not the whole session's history
                    # (which made it re-fire forever once any edit existed).
                    if _is_real_user(o):
                        user_turns += 1
                        edits = 0
                        ship_signals = 0
                        card_actions = 0
                        if need_detect is not None:
                            txt = _user_text(o)
                            if txt:
                                last_user = txt
                                max_needs = max(max_needs,
                                                need_detect.count_needs(txt))
                elif tp == "assistant":
                    edits += _in_scope_edits(o, pr)
                    bash = _bash_cmd(o).lower()
                    if bash:
                        if _is_ship_command(bash):
                            ship_signals += 1
                        if any(m in bash for m in CARD_MARKERS):
                            card_actions += 1
                        # Session-total capture units (#562). Count adds AND
                        # subtask-adds separately (a card.py command may issue
                        # several); "card.py subtask add" does not contain the
                        # "card.py add" substring, so no double counting.
                        capture_units += bash.count("card.py add")
                        capture_units += bash.count("card.py subtask add")
    except OSError:
        pass
    return {
        "edits": edits,
        "ship_signals": ship_signals,
        "card_actions": card_actions,
        "user_turns": user_turns,
        "max_needs": max_needs,
        "capture_units": capture_units,
    }


def load_board(board_path: Path) -> dict:
    try:
        return json.loads(board_path.read_text(errors="replace"))
    except Exception:
        return {}


def _iso(s):
    """Parse an ISO timestamp (tolerating a trailing Z) → datetime, or None."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


# #566 subtask 1 — autonomous (non-interactive) board movers. A rev bump caused
# ONLY by these must NOT be credited as "this session carded its work": the
# background hourly reconcile sweep tags its moves via='harvest' and auto-ship
# tags via='autoship'. An interactive card.py call (main Claude / reconciliation /
# undo) tags agent/recon/undo/etc.
_AUTONOMOUS_VIA = {"harvest", "autoship"}


def _genuine_card_event_since(cards: list, since) -> bool:
    """True iff some card's history has a mutation AFTER `since` that was a
    genuine (interactive) card action — NOT a background reconcile / auto-ship
    move (via in _AUTONOMOUS_VIA). Windows the 'board changed' signal to THIS
    session: a BETWEEN-SESSION background reconcile advances board.rev via
    'harvest' moves, which the old raw rev-delta check wrongly credited as this
    session having carded its work — the #560 audit's missed-gap edge. `since`
    None → False (no baseline window yet; the caller's prev_rev-is-None seeding
    path handles the first encounter)."""
    if since is None:
        return False
    for c in cards:
        for h in (c.get("history") or []):
            if h.get("via") in _AUTONOMOUS_VIA:
                continue
            at = _iso(h.get("at"))
            if at and at > since:
                return True
    return False


def detect_batched(cards: list, since) -> list:
    """Cards that reached Done with no real in-flight dwell — the batched-not-live
    smell (#74). A card is batched if it was BORN in Task (live work, not a
    bootstrap/discovered seed that's born in Done) and then either jumped
    straight to Done without ever passing through inprogress, or sat in
    inprogress < BATCH_DWELL_SEC. Scoped to cards finished at/after `since` (the
    previous Stop), so old cards aren't re-flagged every session. `since` None
    (no baseline yet) → return [] (can't judge a window without one)."""
    if since is None:
        return []
    out = []
    for c in cards:
        if c.get("column") != "done":
            continue
        hist = c.get("history") or []
        if not hist or hist[0].get("to") == "done":
            continue                      # born into Done = historical seed, skip
        done_evt = next((h for h in reversed(hist) if h.get("to") == "done"), None)
        done_at = _iso(done_evt["at"]) if done_evt else _iso(c.get("doneAt"))
        if not done_at or done_at < since:
            continue                      # finished before this window (or undatable)
        ip_evt = next((h for h in hist if h.get("to") == "inprogress"), None)
        if ip_evt is None:
            out.append((c, "never In-Progress (Task→Done jump)"))
            continue
        ip_at = _iso(ip_evt.get("at"))
        if ip_at and (done_at - ip_at).total_seconds() < BATCH_DWELL_SEC:
            dwell = int((done_at - ip_at).total_seconds())
            out.append((c, f"only {dwell}s in In-Progress"))
    return out


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    # Loop guard: if a prior Stop hook is still active, don't re-enter.
    if payload.get("stop_hook_active"):
        return 0

    cwd = payload.get("cwd") or ""
    transcript = payload.get("transcript_path") or ""
    if not cwd:
        return 0
    board_path = find_board(Path(cwd))
    if board_path is None:
        return 0

    # Scope edit-counting to THIS board's project root (#78) so edits to files
    # outside it (memory, other repos) don't trip the un-carded backstop.
    project_root = board_path.parent.parent
    act = scan_transcript(Path(transcript), project_root) if transcript else {
        "edits": 0, "ship_signals": 0, "card_actions": 0, "user_turns": 0,
        "max_needs": 1, "capture_units": 0}

    board = load_board(board_path)
    cards = board.get("cards") or []
    inprogress = [c for c in cards
                  if c.get("column") == "inprogress" and not c.get("doneAt")]

    # Board-rev carding detection (#385 Flaw 2). The robust "did this turn touch
    # the board?" signal: did board.json's rev advance since the last Stop? This
    # is IMMUNE to how card.py was invoked (`$VAR`/alias/wrapper all evade the
    # brittle "card.py add" substring match). Baseline persists per-board across
    # turns AND sessions; the first encounter just seeds it (can't judge a delta
    # with no baseline, so we don't block that one turn).
    cur_rev = board.get("rev")
    now_iso = datetime.now(timezone.utc).isoformat()
    state_path = board_path.parent / ".stop_recon_state.json"
    try:
        _prev = json.loads(state_path.read_text())
        prev_rev = _prev.get("rev")
        prev_at = _iso(_prev.get("at"))
    except Exception:
        prev_rev, prev_at = None, None
    try:
        state_path.write_text(json.dumps({"rev": cur_rev, "at": now_iso}))
    except OSError:
        pass

    # Batched-not-live smell (#74): cards Done this window with no in-flight
    # dwell. Advisory only — never blocks (a batched card is correct end-state,
    # just not live-tracked); surfacing it is what makes the miss self-correct.
    batched = detect_batched(cards, prev_at)
    # Carded if a literal card.py marker was seen this turn (transcript), OR the
    # board genuinely changed since the last sign-off via an INTERACTIVE action
    # (#566 subtask 1: a between-session background reconcile bumps rev via
    # 'harvest' moves — that must NOT count, so we attribute the change by history
    # via, not the raw rev delta). Seeding turn (no prior baseline) counts as
    # carded so we never false-block before the baseline exists.
    genuine_mutation = _genuine_card_event_since(cards, prev_at)
    carded = act["card_actions"] > 0 or genuine_mutation or prev_rev is None

    # Findings — windowed to THIS turn (act) + state-based carding. An existing
    # In-Progress card means the unit IS declared live (#78): the cross-turn
    # pattern (fly inprogress in turn N, edit in N+1, fly done in N+2) left the
    # card in flight, so the edit-heavy middle turn must NOT block. Genuine
    # misses (project edits, no card.py, no rev bump, AND nothing in flight)
    # still block. The deferred In-Progress reminder below still fires.
    uncarded_risk = (
        (act["ship_signals"] > 0 or act["edits"] >= EDIT_THRESHOLD)
        and not carded
        and not inprogress
    )
    # #562 multi-need DROP mirror (non-blocking, SHAPE-NEUTRAL). A prompt this
    # session named ≥2 needs, but fewer were captured on the board (cards +
    # subtasks) than needs — so one may have been left in the conversation. Both
    # correct shapes (1 card + N subtasks, OR N cards) clear this bar equally;
    # it never pushes toward multi-card. Mirror only — never blocks.
    need_gap = (act.get("max_needs", 1) >= 2
                and act.get("capture_units", 0) < act["max_needs"])
    # Nothing worth surfacing → stay silent (don't nag on a read-only session).
    if not uncarded_risk and not inprogress and not batched and not need_gap:
        return 0

    reasons = []
    if uncarded_risk:
        reasons.append(
            f"This session made {act['edits']} file edit(s) and "
            f"{act['ship_signals']} ship-signal(s) (commit/push/'shipped') but "
            f"ran NO card.py add/move/fly — substantive work may be un-carded. "
            f"Create cards for it (Task→In-Progress→Done) per SKILL.md §E/§J."
        )
    if batched:
        bl = ", ".join(f"#{c.get('num')} ({why})" for c, why in batched[:8])
        reasons.append(
            f"{len(batched)} card(s) reached Done with no live in-flight tracking "
            f"this session ({bl}) — the batched (add→done) smell, not live "
            f"task→In-Progress→done. Not an error; a nudge to declare work UP "
            f"FRONT next time (card + `fly inprogress` BEFORE editing) per "
            f"SKILL.md 'The three laws' (law #1)."
        )
    if inprogress:
        ip = ", ".join(f"#{c.get('num')} {c.get('code') or c.get('title','')[:30]}"
                       for c in inprogress[:8])
        reasons.append(
            f"{len(inprogress)} card(s) still In-Progress at sign-off ({ip}). "
            f"Confirm each is actually done (→ move to done w/ writeup) or leave "
            f"a note on why it's still open."
        )
    if need_gap:
        reasons.append(
            f"A prompt this session named ~{act['max_needs']} needs but only "
            f"{act['capture_units']} were captured on the board (cards + subtasks) "
            f"— a need may have been left in the conversation. Confirm ALL of them "
            f"are on the board (in whatever shape the header test gives — one card "
            f"+ subtasks OR separate cards), or note why fewer. (Carding LAW — "
            f"capture up front; SKILL.md shape banner.)"
        )

    payload_out = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "source": "stop_recon",
        "board": str(board_path),
        "session_id": payload.get("session_id"),
        "instructions": (
            "Session-end reconciliation backstop (#279). The PREVIOUS session "
            "ended with possible gaps. Review the reasons below against your "
            "memory of that work, then either create/move the missing cards via "
            "`card.py add/move/fly` or, if everything is actually fine, just "
            "delete this file. Stay-by-default — don't invent cards."
        ),
        "activity": act,
        "reasons": reasons,
        "inprogress": [{"num": c.get("num"), "title": c.get("title"),
                        "code": c.get("code")} for c in inprogress],
        "batched": [{"num": c.get("num"), "title": c.get("title"),
                     "why": why} for c, why in batched],
    }
    try:
        (board_path.parent / "recon_pending.json").write_text(
            json.dumps(payload_out, indent=2, ensure_ascii=False))
    except OSError:
        pass

    # BLOCKING backstop — ADVISORY BY DEFAULT (#592). The recon_pending.json note
    # above is always written, so a genuine un-carded miss is caught at the NEXT
    # SessionStart regardless. The same-turn *blocking* force (refuse to stop and
    # make Claude card it now) is opt-in via BOARD_STEWARD_STRICT=1 — it costs an
    # extra model turn and is user-visible (the forced continuation), which is
    # noise for shipped users. Power users who want the hard live guarantee set
    # the env flag. When it does fire it fires at most ONCE per stop: the
    # stop_hook_active loop-guard above short-circuits the forced continuation.
    strict = os.environ.get("BOARD_STEWARD_STRICT", "").strip().lower() \
        not in ("", "0", "false", "no", "off")
    if uncarded_risk and strict:
        block_reason = (
            "Board-steward LIVE backstop: this turn made "
            f"{act['edits']} file edit(s) / {act['ship_signals']} ship-signal(s) "
            "but ran NO card.py add/move/fly — the work is un-carded. Before you "
            "finish, card it now: `card.py add --column task --title \"<verb+noun>\"` "
            "→ `card.py fly <n> inprogress` → `card.py fly <n> done --writeup "
            "\"<commits/files/verification>\"`. If this turn was genuinely "
            "read-only/explanatory, say so in one line and stop again."
        )
        print(json.dumps({"decision": "block", "reason": block_reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
