#!/usr/bin/env python3
"""hourly_extractor post-extraction reconciliation sweep — extracted from hourly_extractor.py (#307).

After cards are emitted, re-scan the recent activity for skip/nvm/done signals
and move cards accordingly (its own _RECON_PROMPT / LLM pass). Depends on the
digest builder + LLM constants (hourly_common) and the progress-banner updater
(hourly_emit) — both leaves, so no circular import back to hourly_extractor.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hourly_common import *  # noqa: E402,F401,F403  (build_digest, _CLAUDE_BIN, _LLM_MODEL)
from hourly_emit import *     # noqa: E402,F401,F403  (_banner_update)
import _boardio  # noqa: E402  (recon_lock — serialize concurrent reconcile passes)


# ---------- post-extraction reconciliation sweep ----------

_RECON_PROMPT = """\
You are reconciling a kanban board against the user's recent activity.

Below: cards currently in NON-DONE columns of the board, with their titles, bucket timestamps, and notes.
After that: the chronological activity log from the same time window.

For each card, decide its TRUE STATUS based on whether the user (in the activity log) later:
- Said "skip", "nvm", "don't do that", "we won't ship this", "defer", "later" → MOVE to backlog
- Said "done", "we shipped it", or there is a commit/ship hit matching the card's noun cluster → MOVE to done
- Said "urgent", "must", "this is impt", "critical", "asap", "p0", "p1", "blocker" → MOVE to super-urgent
- No clear later signal AND card matches active work → STAY
- Sat untouched > 24h with no follow-up → MOVE to backlog (stale)

Return ONLY a JSON array (no markdown). One object per card you have a confident judgment on (omit cards you'd keep as STAY):
[
  {"num": 42, "target": "backlog", "reason": "user said 'lets skip this for now'"},
  {"num": 73, "target": "done", "reason": "commit cd9f9a1 lands the work"}
]

Skip cards whose right column is unclear. Be conservative — only move when the signal is clear.
"""


def _build_recon_card_block(cards: list[dict]) -> str:
    lines: list[str] = []
    for c in cards:
        bucket_ts = c.get("createdAt", "")
        title = c.get("title", "")[:80]
        notes = (c.get("notes") or "").replace("\n", " ")[:240]
        lines.append(f"  #{c['num']} [{c['column']}] @{bucket_ts[:16]} — {title}")
        if notes:
            lines.append(f"      notes: {notes}")
    return "\n".join(lines)


def _build_activity_digest(events: list[dict], max_chars: int = 8000) -> str:
    """Compact chronological digest of user prompts + commits for the recon LLM call."""
    lines: list[str] = []
    for ev in sorted(events, key=lambda e: e["ts"]):
        kind = ev["kind"]
        ts = ev["ts"].strftime("%m-%d %H:%M")
        if kind in ("user_prompt", "convo_user"):
            text = (ev.get("text") or "").strip().replace("\n", " ")[:200]
            if text:
                lines.append(f"  [{ts}] USER: {text}")
        elif kind == "git_commit":
            sha = (ev.get("meta") or {}).get("shaShort", "")
            lines.append(f"  [{ts}] COMMIT {sha}: {ev['text'][:100]}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        # Keep the END (most recent) so user's later "nvm" calls survive.
        out = "…[earlier truncated]…\n" + out[-max_chars:]
    return out


def _llm_reconcile(cards: list[dict], events: list[dict],
                   timeout_s: int = 90) -> list[dict]:
    """Run one LLM call. Returns list of {num, target, reason}."""
    card_block = _build_recon_card_block(cards)
    activity = _build_activity_digest(events)
    full = (
        f"{_RECON_PROMPT}\n\n"
        f"--- CARDS ({len(cards)}) ---\n{card_block}\n\n"
        f"--- ACTIVITY LOG ---\n{activity}\n"
    )
    try:
        proc = subprocess.run(
            _LLM_ARGS,   # shared argv: thinking-off (env) + --strict-mcp-config
            input=full, capture_output=True, text=True, timeout=timeout_s,
            env=_LLM_ENV,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"  ! recon LLM call failed: {e}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        return []
    # Shared robust parser (hourly_common) — tolerates prose wrappers, embedded
    # chat turns the model answered, and a truncated tail (#324).
    moves = parse_card_array(proc.stdout)
    if moves is None:
        print(f"  recon LLM returned no parseable JSON array: "
              f"{(proc.stdout or '')[:200]!r}", file=sys.stderr)
        return []
    return moves


def _emit_recon_pending(board: Path, candidates: list[dict],
                         events: list[dict], card_py: Path,
                         banner_num: int | None) -> int:
    """Write recon_pending.json for main Claude to action. Returns 0
    (recon hasn't happened yet — the file is the deliverable). Main Claude
    reads the file next turn, decides moves, calls card.py fly, and
    deletes the file when done."""
    pending_path = board.parent / "recon_pending.json"
    activity = _build_activity_digest(events, max_chars=12000)
    payload = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "board": str(board),
        "card_py": str(card_py),
        "instructions": (
            "You (main Claude) have the full session context. Read this "
            "file, decide which of the listed cards should move based on "
            "the activity log AND your conversation memory, then apply "
            "moves via `card.py fly <num> <col> [--writeup TEXT] "
            "[--note TEXT]`. Delete this file "
            "when done. Stay-by-default — only move when a clear signal "
            "(user said skip/nvm/abandoned/we shipped it / matching commit)."
        ),
        "candidates": [
            {
                "num": c["num"],
                "column": c["column"],
                "title": c["title"],
                "notes": c.get("notes") or "",
                "createdAt": c.get("createdAt"),
                "tags": c.get("tags") or [],
            }
            for c in candidates
        ],
        "activity_digest": activity,
    }
    try:
        pending_path.write_text(json.dumps(payload, indent=2,
                                            ensure_ascii=False))
        print(f"📋 wrote {len(candidates)} recon candidates → {pending_path}\n"
              f"   (CLAUDECODE=1 detected — main Claude will reconcile "
              f"next turn, no Haiku call)", file=sys.stderr)
        if banner_num:
            _banner_update_text(card_py, board, banner_num,
                                f"📋 {len(candidates)} cards waiting for "
                                f"main-Claude recon")
    except OSError as e:
        print(f"  ! recon_pending write failed: {e}", file=sys.stderr)
    return 0


def _emit_extraction_pending(board: Path, card_py: Path,
                              chunks: list[list[int]],
                              buckets: dict, bucket_min: int,
                              project: Path) -> int:
    """INLINE extraction (the free, no-Haiku default — #247). Instead of
    spawning `claude -p haiku` per chunk, stage the bucketed digests in
    extraction_pending.json. Main Claude — the session the user is already
    in — reads it and emits the cards itself, at zero extra cost and higher
    quality than Haiku. Returns the number of chunks staged."""
    pending_path = board.parent / "extraction_pending.json"
    staged = []
    # #299: ONE seen-set across ALL chunks. Main Claude reads every staged digest
    # in a single context, so a non-signal head repeated in two different buckets
    # is redundant for the reader — dedup it end-to-end, not just per chunk.
    seen_heads: set = set()
    for ck in chunks:
        ev = [e for k in ck for e in buckets.get(k, [])]
        if not ev:
            continue
        label = ", ".join(_bucket_label(k, bucket_min) for k in ck)
        ts_iso = datetime.fromtimestamp(
            min(ck) * bucket_min * 60, tz=timezone.utc).isoformat()
        staged.append({
            "label": label,
            "bucket_ts_iso": ts_iso,
            "digest": build_digest(ev, project, seen_heads=seen_heads),
        })
    payload = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "board": str(board),
        "card_py": str(card_py),
        "card_format": _LLM_PROMPT,
        "instructions": (
            "INLINE board extraction (free — no Haiku). You (main Claude) have "
            "full context, so produce BETTER cards than Haiku would. For EACH "
            "chunk in `chunks` below: read its `digest`, identify the discrete "
            "units of work exactly per `card_format`, and emit each as a card:\n"
            "  python3 <card_py> --board <board> add --column task --priority PRIO "
            "--title T [--code CODE] --origin O --notes N --created-at <bucket_ts_iso> [--tag T]\n"
            "Born in 'task', then FLY through the lifecycle so it glides (don't plop):\n"
            "  done card → TWO hops (lays in In Progress, auto-adds the ☑ initial-ship subtask):\n"
            "    python3 <card_py> --board <board> fly <num> inprogress --pause-ms 400\n"
            "    python3 <card_py> --board <board> fly <num> done --pause-ms 400 --writeup <notes>\n"
            "  inprogress card → one hop (fly <num> inprogress). backlog/mandatory/notes → leave there.\n"
            "  RICHER PATH (#294 — only when the digest SHOWS it, never invented): if a done card later\n"
            "  BROKE (regression/revert/reopen) and was fixed, reconstruct the real bounce —\n"
            "    python3 <card_py> --board <board> fly <num> inprogress --bug \"<what broke>\"  (adds 🐞 subtask + bug tag)\n"
            "    python3 <card_py> --board <board> fly <num> done --writeup \"<the fix>\"        (bug tag auto-strips)\n"
            "  For an ENHANCEMENT after ship use `--improve \"<what's added>\"` instead of `--bug`. Each hop is\n"
            "  recorded in history[] (#258), so the board mirrors the TRUE lifecycle, not a flat task→IP→done.\n"
            "Process chunks NEWEST-FIRST; dedupe a multi-chunk effort into ONE card. Keep "
            "titles clean (code is a separate badge), cite commit SHAs in notes "
            "when a COMMIT line is in the digest, and only assign a code to a "
            "distinctly-named feature/system/fix.\n"
            "PROGRESS HUD (#318 — so the user watches the board fill live): the moment you "
            "START, run `python3 <card_py> --board <board> progress --done 0 --total <N>` "
            "where N = the number of chunks below; then AFTER finishing each chunk run "
            "`python3 <card_py> --board <board> progress --done <k> --total <N> --label \"<that chunk's bucket_ts_iso window>\"`. "
            "It's best-effort (no-op if no live server) — never skip a card to do it.\n"
            "COMPLETENESS SWEEP (never miss a point — priority super-urgent > notes > backlog): "
            "after emitting, re-scan EVERY digest for the categories with NO commit "
            "marker that a ship-oriented read drops — (1) urgency the user voiced "
            "('this is impt'/must/urgent/asap/p0/blocker) → a 'super-urgent' card; "
            "(2) a decision/rationale/observation → a 'notes' card; (3) deferrals "
            "('later'/'next session'/'defer'/'nvm save it') → a 'backlog' card with a "
            "'⏸ OPEN — <what remains>' note. Add any that didn't already become a card; "
            "super-urgent first. ONLY DELETE this file AFTER both the per-chunk emit AND the "
            "completeness sweep above are done — a leftover file is the session-start signal "
            "(#315) that the sweep was skipped, so deleting it early defeats the guard."
        ),
        "chunks": staged,
    }
    # #293 INLINE TOKEN GUARD — inline funnels EVERY chunk digest through the
    # main session's context, so a big window can eat much of a small-context /
    # free tier's budget (haiku has no such cost: chunked + background + Haiku-
    # priced). Warn loudly with the estimate so the user can cut the window or
    # switch fills. Estimate = digest chars/4 (→tokens) × 1.4 (prompt overhead).
    # Threshold overridable via BOARD_INLINE_WARN_TOKENS (default 100k — the
    # danger zone for a ~200k free-tier context once the live convo is added).
    total_chars = sum(len(s["digest"]) for s in staged)
    est_tokens = int(total_chars / 4 * 1.4)
    warn_at = int(os.environ.get("BOARD_INLINE_WARN_TOKENS", "100000"))
    if est_tokens > warn_at:
        print(f"\n  ⚠️  INLINE COST ~{est_tokens:,} tokens across {len(staged)} chunk(s) — "
              f"read through THIS session's context.\n"
              f"      On a small-context / free tier this can consume much of your budget.\n"
              f"      Cheaper: narrow the window (e.g. --harvest-days 1 or 2), or use "
              f"--fill haiku (chunked, background, ~12× cheaper, no main-context cost).\n",
              file=sys.stderr)
    try:
        pending_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"📋 staged {len(staged)} chunk(s) → {pending_path}\n"
              f"   (inline mode — main Claude emits the cards, no Haiku cost)",
              file=sys.stderr)
    except OSError as e:
        print(f"  ! extraction_pending write failed: {e}", file=sys.stderr)
        return 0
    return len(staged)


def reconcile_sweep(card_py: Path, board: Path, events: list[dict],
                     banner_num: int | None = None,
                     only_discovered: bool = True) -> int:
    """LLM sweep on non-done cards. Asks LLM if any should move based on the
    activity log. Applies moves. Returns count moved.

    `only_discovered` (default True) restricts candidates to bootstrap-mined
    cards (tagged 'discovered') — the right scope for the post-extraction
    bootstrap sweep, which must not molest the user's hand-made cards while it
    fills the board. The SessionStart recon passes only_discovered=False to
    reconcile EVERY non-done card (the live In-Progress cards from the last
    session — created by `card.py add`, so untagged — are exactly what needs
    IP→done / →mandatory truth-making).

    When CLAUDECODE=1 (we're running inside an active Claude Code session),
    skip the Haiku subprocess entirely. Main Claude already has the full
    conversation in context — write a recon_pending.json that main Claude
    actions next turn. Saves a 60-90s LLM call + tokens, and lets recon
    use the richer session context the script doesn't have. (The autonomous
    background spawns — bootstrap + SessionStart — unset CLAUDECODE so they
    take the Haiku path, not this one.)"""
    try:
        with board.open("r") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0

    # Non-done, non-banner cards from columns we want to reconcile. When
    # only_discovered, restrict to bootstrap-mined cards (see docstring).
    candidates = [
        c for c in state.get("cards", [])
        if c.get("column") in ("task", "backlog", "inprogress", "notes")
        and "banner" not in (c.get("tags") or [])
        and (not only_discovered or "discovered" in (c.get("tags") or []))
    ]
    if not candidates:
        return 0

    # Inline-recon path: write TODO and let main Claude action it.
    if os.environ.get("CLAUDECODE") == "1":
        return _emit_recon_pending(board, candidates, events,
                                    card_py, banner_num)

    # Autonomous path: subprocess Haiku. Serialize per board — a SECOND concurrent
    # reconcile (e.g. the bootstrap end-of-replay sweep + a SessionStart recon-only
    # firing in the same window) is what produced the "✓ already up to date" then
    # cards-shuffle then "N brought up to date", twice. recon_lock bails (no wait)
    # if another pass already holds it; the in-flight pass already covers it.
    with _boardio.recon_lock(board) as got_lock:
        if not got_lock:
            print("  recon: another reconcile is already running — skip",
                  file=sys.stderr)
            return 0

        print(f"▶ reconciliation sweep: {len(candidates)} non-done card(s)…",
              file=sys.stderr)
        # Live HUD line so the user knows WHY cards are about to move (#recon-hud) —
        # shown on both the bootstrap fill HUD and a SessionStart recon.
        # Short enough to fit the ~330px HUD window line (the old 62-char copy was
        # cut off mid-sentence). Present-tense ACTION — the sweep is still running, so
        # it can't yet claim an outcome ("nothing missed" is the result, shown on the
        # ✓ final line below). Header already says "reconciling"; this is the why.
        _emit_progress(card_py, board, 0, 1,
                       "checking nothing's missed…", "reconcile")
        if banner_num:
            _banner_update_text(card_py, board, banner_num,
                                f"🔍 reconciling {len(candidates)} cards…")

        moves = _llm_reconcile(candidates, events)
        if not moves:
            print("  recon: 0 moves", file=sys.stderr)
            _emit_progress(card_py, board, 1, 1,
                           "✓ already up to date — nothing to move", "reconcile",
                           final=True)
            return 0

        n_moved = 0
        for m in moves:
            num = m.get("num")
            target = m.get("target")
            reason = (m.get("reason") or "")[:160]
            if not isinstance(num, int) or target not in (
                    "task", "backlog", "inprogress", "done", "super-urgent"):
                continue
            # Find current column
            cur = next((c for c in candidates if c["num"] == num), None)
            if not cur:
                continue
            if cur["column"] == target:
                continue
            # #506 — these moves are the automated background harvester, not a
            # hands-on move; tag them so the Logs HUD shows (Auto-harvest) MOVE.
            args = [sys.executable, str(card_py), "--board", str(board),
                    "fly", str(num), target, "--pause-ms", "150", "--via", "harvest"]
            if target == "done":
                args += ["--writeup", f"Recon: {reason}"]
            else:
                args += ["--note", f"Recon → {target}: {reason}"]
            try:
                out = subprocess.run(args, capture_output=True, text=True, timeout=8)
            except subprocess.SubprocessError:
                continue
            if out.returncode == 0:
                n_moved += 1
                print(f"  recon: #{num} → {target}  ({reason[:60]})",
                      file=sys.stderr)
        print(f"  recon: {n_moved} card(s) moved", file=sys.stderr)
        _emit_progress(card_py, board, 1, 1,
                       f"✓ {n_moved} card(s) brought up to date", "reconcile",
                       final=True)
        return n_moved



__all__ = [
    "_RECON_PROMPT", "_build_recon_card_block", "_build_activity_digest",
    "_llm_reconcile", "_emit_recon_pending",
    "_emit_extraction_pending", "reconcile_sweep",
]
