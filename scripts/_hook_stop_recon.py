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
import sys
from datetime import datetime, timezone
from pathlib import Path

EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
EDIT_THRESHOLD = 3          # this many edits w/ no card = uncarded-work signal
SHIP_RE_WORDS = ("git commit", "shipped", "deployed", "merged", "git push")
CARD_MARKERS = ("card.py add", "card.py move", "card.py fly", "card.py improve",
                "card.py bug", "card.py auto-ship", "card.py subtask")
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


def scan_transcript(path: Path) -> dict:
    """Tally this session's activity from its own transcript jsonl."""
    edits = 0
    ship_signals = 0
    card_actions = 0
    user_turns = 0
    last_user = ""
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
                elif tp == "assistant":
                    names = _tool_name(o)
                    if names:
                        for n in names.split():
                            if n in EDIT_TOOLS:
                                edits += 1
                    bash = _bash_cmd(o).lower()
                    if bash:
                        if any(w in bash for w in SHIP_RE_WORDS):
                            ship_signals += 1
                        if any(m in bash for m in CARD_MARKERS):
                            card_actions += 1
    except OSError:
        pass
    return {
        "edits": edits,
        "ship_signals": ship_signals,
        "card_actions": card_actions,
        "user_turns": user_turns,
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

    act = scan_transcript(Path(transcript)) if transcript else {
        "edits": 0, "ship_signals": 0, "card_actions": 0, "user_turns": 0}

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
    rev_advanced = (isinstance(cur_rev, int) and isinstance(prev_rev, int)
                    and cur_rev > prev_rev)
    # Carded if the board actually changed (rev) OR a literal marker was seen
    # (belt-and-suspenders). Seeding turn (no prior baseline) counts as carded
    # so we never false-block before the baseline exists.
    carded = rev_advanced or act["card_actions"] > 0 or prev_rev is None

    # Findings — windowed to THIS turn (act) + state-based carding.
    uncarded_risk = (
        (act["ship_signals"] > 0 or act["edits"] >= EDIT_THRESHOLD)
        and not carded
    )
    # Nothing worth surfacing → stay silent (don't nag on a read-only session).
    if not uncarded_risk and not inprogress and not batched:
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

    # BLOCKING backstop (the LIVE 100% guarantee). If this turn did substantive
    # work but ran no card.py action, refuse to end the turn and tell Claude to
    # card it NOW. Emitting {"decision":"block","reason":...} on stdout is the
    # Claude Code Stop-hook contract for "don't stop yet". This can fire at most
    # ONCE per stop: on the forced continuation Claude Code sets
    # stop_hook_active=true, which the loop guard above short-circuits to exit 0.
    # An inprogress-only gap (no edits/ships) is NOT blocking — it just left a
    # deferred recon_pending.json note above, so a card legitimately in flight
    # across sessions never traps the user.
    if uncarded_risk:
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
