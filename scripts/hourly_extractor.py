#!/usr/bin/env python3
"""hourly_extractor.py — bucket 1 hour at a time, LLM digest → cards.

Replaces the per-turn extraction model. For each 1-hour bucket of activity:
  1. Build a digest of all events in that hour (prompts, edits, commits, etc.)
  2. Call `claude -p` headlessly with a structured prompt
  3. LLM returns a JSON array of cards (work units, NOT per-turn cards)
  4. Emit each card via card.py with optional lifecycle flight

This is the "simulate the work as if a human was titling cards" model — one
card per discrete unit of work, with column routing inferred from signals
(commits → done, urgency phrases → mandatory, file edits → inprogress, etc.)

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover2 import (
    harvest_jsonl, harvest_convo, harvest_git, harvest_memory, harvest_plans,
    harvest_history, parse_ts, find_convo_dir,
)

_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
_LLM_MODEL = os.environ.get("HOURLY_MODEL", "haiku")

# ---------- LLM digest prompt ---------------------------------------------

_LLM_PROMPT = """\
You are extracting kanban cards from a block of work activity. The input below is a chronological log from one OR more time buckets.

Your job: identify the DISCRETE UNITS OF WORK that happened in each bucket. Each unit becomes ONE card. Group related turns (the user asked, then clarified, then you built it, then they reviewed) under ONE card — NOT one card per turn.

Output: a JSON ARRAY of card objects. Each card:
{
  "title": "verb + noun phrase, ≤70 chars. CLEAN — do NOT prefix with the code (the code renders as its own badge). Examples: 'Atomic-hop primitive for card moves', 'Fix card-drag freeze on iPhone', 'Investigate convo dedup'. NO conversational openers (btw, can u, oh wait). NO verbatim user wording — summarize the WORK.",
  "code": "short CAPS badge from the noun cluster, ≤24 chars (e.g. 'BOARD-FLY', 'DISCOVER2', 'SIM-60D'). Assign one ONLY when the work has a distinct, reusable NAMED subject — a feature, system, or named fix you'd reference again (roughly half of cards earn one). Leave it EMPTY for routine one-off fixes, chores, tweaks, investigations, or observations with no nameable subject. A code means 'this is a thing with a name', not 'something happened'.",
  "column": "one of: task | backlog | inprogress | done | mandatory | notes",
  "priority": "low | mid | critical",
  "origin": "WHY this work exists — the user's goal or the trigger, in their voice/intent (not yours). ≤200 chars. e.g. 'User wanted card-drag to work on iPhone where the columns stack vertically and the old handler froze.' This is the 'why this exists' a teammate reads to understand the card at a glance. Empty string only if genuinely unknowable.",
  "notes": "What the work actually was: problem → approach → outcome (or current state). 1-3 sentences, ≤300 chars. Concrete — name the file/function/command. If a COMMIT line (a sha) for this work appears in the bucket log, ALWAYS cite its short sha, e.g. 'Shipped in 7b565ff.' For UNFINISHED work, state what's left. Empty string only if no signal.",
  "tags": ["one or two from: feature | bug | fix | refactor | doc | design | discipline | infrastructure"],
  "transitions": "OPTIONAL ordered array of EXTRA lifecycle hops AFTER the first ship — reconstruct the TRUE path the card took, but ONLY when the digest explicitly shows it. Each entry: {\"to\": \"inprogress\"|\"done\", \"kind\": \"bug\"|\"improve\"|null, \"reason\": \"short text ≤80 chars\"}. kind 'bug' = the shipped card BROKE (regression/revert/reopen in the log) and flew back to In Progress to be fixed; 'improve' = an enhancement after ship. A reopen is normally followed by a {\"to\":\"done\"} hop. OMIT or [] for the normal task→IP→done (most cards). NEVER invent a bug cycle the digest doesn't show."
}

Column routing rules:
- "done"       → a git commit landed in this hour OR a clean ship phrase appeared (shipped X / deployed / merged)
- "mandatory"  → user said urgent / must / impt / critical / asap / blocker / 'this is impt'
- "inprogress" → files were edited but no ship hit
- "task"       → mentioned, named, planned but no edits yet
- "backlog"    → deferred / open / undone: user said "later" / "next session" / "tomorrow" / "defer" / "pending" / "we'll revisit" / "nvm save it", OR the work was started but explicitly NOT finished
- "notes"      → captured observation / idea / decision, NOT a unit of work to ship

OPEN / DEFERRED work is the highest-value signal — surface it, don't bury it:
- If the work was deferred or left unfinished, route to "backlog" (or "task" if never started) AND begin notes with "⏸ OPEN — " followed by exactly what remains and the trigger to resume (e.g. "⏸ OPEN — sim_60d --strict still fails on the archive-on-install gap; resume to decide strict-cap policy.").
- Closure markers ("shipped" / "merged" / "done" / a commit sha) override deferral — those go to "done".

Lifecycle transitions (reconstruct the TRUE path, not just the final state):
- Most cards are a plain task→IP→done (or just end in their column). Leave "transitions" empty/omitted.
- BUT if the digest shows a card was shipped, then BROKE — a regression, revert, reopen, or "X broke / bug in X after we shipped" — and was fixed, add a "bug" transition then a "done". If it was ENHANCED after ship, add an "improve" transition then a "done". This makes the board carry the real 🐞 / ✨ subtasks + history[] the live board would have had.
- Only reconstruct hops you can SEE in the log. NEVER fabricate a bug bounce to look thorough.

Quality bar:
- Skip conversational micro-turns ("yes", "ok", "stop", "open the board", "rerun"). They are NOT cards.
- One unit of work = one card. If the user asked about feature X, you built it, and they reviewed it — that is ONE card titled by what X is.
- If two units of work happened in the same hour, return two cards.
- origin = the user's WHY; notes = the WHAT/HOW/STATE. Keep them distinct, both concrete. Prefer real file/commit/command names over vague summaries.
- If nothing card-worthy happened, return [].

Return ONLY the JSON array. NO markdown, NO commentary, NO ```json fences.
"""


# ---------- digest builder ------------------------------------------------

def _bucket_hour(ts: datetime, bucket_min: int = 60) -> int:
    return int(ts.timestamp()) // (bucket_min * 60)


def _bucket_label(bucket: int, bucket_min: int = 60) -> str:
    dt = datetime.fromtimestamp(bucket * bucket_min * 60, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ---------- #299 DIGEST-COMPACT: lossless digest compaction ----------------
# build_digest is 65% assistant "head" lines, and a large share of those carry
# ZERO work signal — code-fence artifacts from convo dumps (```json / bare [ ]),
# short connective tissue ("Sync + re-test:", "Done.", "Smoke test:"), and exact
# repeats. We drop ONLY those, keeping every survivor BYTE-IDENTICAL.
#
# HARD GATE (protects the "never miss a point" star feature): a head is dropped
# ONLY if it has NO work signal AND is either a code-fence artifact or a short
# (≤45-char) connector. USER prompts, COMMIT / file-edit / MEMORY / PLAN lines,
# and any head carrying a sha / card-ref / file / §-rev / decision figure are
# NEVER dropped — even when they look like chatter. Set DIGEST_COMPACT=0 to
# disable (used for A/B measurement; on by default).
_COMPACT = os.environ.get("DIGEST_COMPACT", "1") != "0"

# Work-signal tokens. If a head matches any, it is KEPT no matter its shape.
_SIGNAL_RE = re.compile(
    r"`[^`]+`"                                   # `code`/`file` spans
    r"|/[\w./-]+"                                 # path-like tokens
    r"|\b[\w-]+\.(py|md|json|js|html|sh|txt|css|yml|yaml|toml|service|plist|cfg)\b"
    r"|\b[0-9a-f]{7,40}\b"                        # commit shas
    r"|#\d+"                                      # card refs
    r"|§|\brev\b|\bHEAD\b"                        # section / rev / HEAD
    r"|\b\d{2,}\b",                               # counts / result figures / ports
    re.I,
)
# Lines that are pure convo-dump artifacts (an assistant message that opened with
# a JSON block → the harvested "head" is just the fence/bracket). Never signal.
_CODE_ARTIFACT_RE = re.compile(r"^(`{3}\w*|\[\]?|\]|\{\}?|\}|json)$")


def _head_droppable(head: str) -> bool:
    """True iff this assistant head is safe-to-drop boilerplate: NO work signal
    AND (a code-fence artifact OR a short ≤45-char connector). Conservative by
    design — long lines are kept even when they open with 'Now…'/'Stopped…'
    because they routinely continue into a real decision."""
    t = head.strip()
    if not t:
        return True
    if _SIGNAL_RE.search(t):
        return False
    if _CODE_ARTIFACT_RE.match(t):
        return True
    return len(t) <= 45


def _detime(line: str) -> str:
    """Strip the leading '  [HH:MM:SS] ' so identical heads at different times
    collapse to one dedup key."""
    return re.sub(r"^\s*\[\d\d:\d\d:\d\d\]\s*", "", line).strip()


def build_digest(bucket_events: list[dict], project: Path,
                 seen_heads: set | None = None) -> str:
    """Compact chronological digest of an hour of events for the LLM.

    #299: applies lossless compaction (drop boilerplate heads, collapse
    consecutive dups, cross-bucket-dedup repeated non-signal heads). Pass a
    shared `seen_heads` set across buckets in one chunk to dedup across them.
    """
    lines: list[str] = []
    for ev in bucket_events:
        ts = ev["ts"].strftime("%H:%M:%S")
        kind = ev["kind"]
        if kind in ("user_prompt", "convo_user"):
            txt = (ev.get("text") or "").strip().replace("\n", " ")[:400]
            lines.append(f"  [{ts}] USER: {txt}")
        elif kind in ("asst_msg", "convo_asst"):
            txt = (ev.get("text") or "").strip()
            # Just the head — full asst replies are too long
            head = txt.split("\n", 1)[0][:300]
            files = ev.get("files") or []
            if files:
                fnames = ", ".join(Path(f).name for f in files[:5])
                lines.append(f"  [{ts}] CLAUDE edited: {fnames}")
            if head:
                # #299: drop pure-boilerplate heads (lossless — no work signal).
                if _COMPACT and _head_droppable(head):
                    continue
                # #299: cross-bucket dedup of repeated NON-signal heads. Signal
                # heads are kept even if repeated (a repeat is meaningful there).
                if _COMPACT and seen_heads is not None \
                        and not _SIGNAL_RE.search(head):
                    key = head.strip()
                    if key in seen_heads:
                        continue
                    seen_heads.add(key)
                lines.append(f"  [{ts}] CLAUDE: {head}")
        elif kind == "git_commit":
            sha = (ev.get("meta") or {}).get("shaShort", "")
            lines.append(f"  [{ts}] COMMIT {sha}: {ev['text'][:120]}")
        elif kind == "memory_write":
            lines.append(f"  [{ts}] MEMORY: {ev['text']}")
        elif kind == "plan_write":
            lines.append(f"  [{ts}] PLAN: {ev['text']}")
    # #299: collapse consecutive identical CLAUDE-head lines (a repeated chatter
    # line carries no new info). Protected line types (USER / COMMIT / edited /
    # MEMORY / PLAN) are left untouched so the hard gate stays provably clean —
    # even an exact-dup file line is kept rather than risk the never-miss promise.
    if _COMPACT:
        deduped: list[str] = []
        for ln in lines:
            if (deduped and ln == deduped[-1]
                    and "] CLAUDE:" in ln):   # only fold head-line dupes
                continue
            deduped.append(ln)
        lines = deduped
    return "\n".join(lines)


# ---------- LLM dispatch --------------------------------------------------

def extract_cards_for_hour(bucket_events: list[dict], project: Path,
                            bucket_label: str,
                            timeout_s: int = 60) -> list[dict]:
    """Single-bucket extraction (legacy path; --chunk-size 1)."""
    return extract_cards_for_chunk(
        [(bucket_label, bucket_events)], project, timeout_s=timeout_s)


def extract_cards_for_chunk(chunk: list[tuple[str, list[dict]]],
                             project: Path,
                             timeout_s: int = 90) -> list[dict]:
    """Multi-bucket extraction. chunk = [(bucket_label, events), ...] in time
    order. Builds a combined digest with bucket headers, sends ONE LLM call,
    returns a flat card array. Pays the claude -p cold-start once per chunk
    instead of per bucket."""
    sections: list[str] = []
    seen_heads: set = set()   # #299: cross-bucket dedup within this chunk
    for label, bevents in chunk:
        digest = build_digest(bevents, project, seen_heads=seen_heads)
        if not digest.strip():
            continue
        sections.append(f"=== BUCKET {label} ===\n{digest}")
    if not sections:
        return []
    combined = "\n\n".join(sections)
    full = (
        f"{_LLM_PROMPT}\n\n"
        f"--- WORK ACTIVITY ({len(chunk)} bucket(s), project={project.name}) ---\n"
        f"{combined}\n"
    )
    label_summary = " + ".join(label for label, _ in chunk)
    try:
        proc = subprocess.run(
            [_CLAUDE_BIN, "-p", "--output-format", "text",
             "--model", _LLM_MODEL],
            input=full, capture_output=True, text=True, timeout=timeout_s,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"  ! LLM call failed for chunk [{label_summary}]: {e}",
              file=sys.stderr)
        return []
    if proc.returncode != 0:
        print(f"  ! claude -p exit {proc.returncode} for chunk [{label_summary}]",
              file=sys.stderr)
        return []
    out = (proc.stdout or "").strip()
    out = re.sub(r"^```(?:json)?\s*", "", out)
    out = re.sub(r"\s*```\s*$", "", out)
    try:
        cards = json.loads(out)
        if not isinstance(cards, list):
            return []
        return cards
    except json.JSONDecodeError:
        print(f"  ! LLM returned non-JSON for chunk [{label_summary}]",
              file=sys.stderr)
        return []


# ---------- card emission -------------------------------------------------

# ---------- post-extraction reconciliation sweep ----------

_RECON_PROMPT = """\
You are reconciling a kanban board against the user's recent activity.

Below: cards currently in NON-DONE columns of the board, with their titles, bucket timestamps, and notes.
After that: the chronological activity log from the same time window.

For each card, decide its TRUE STATUS based on whether the user (in the activity log) later:
- Said "skip", "nvm", "don't do that", "we won't ship this", "defer", "later" → MOVE to backlog
- Said "done", "we shipped it", or there is a commit/ship hit matching the card's noun cluster → MOVE to done
- Said "urgent", "must", "this is impt", "critical", "asap", "p0", "p1", "blocker" → MOVE to mandatory
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
            [_CLAUDE_BIN, "-p", "--output-format", "text",
             "--model", _LLM_MODEL],
            input=full, capture_output=True, text=True, timeout=timeout_s,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"  ! recon LLM call failed: {e}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        return []
    out = (proc.stdout or "").strip()
    out = re.sub(r"^```(?:json)?\s*", "", out)
    out = re.sub(r"\s*```\s*$", "", out)
    # The LLM often appends commentary after the JSON array even though the
    # prompt says not to. Extract the first balanced JSON array (greedy from
    # first '[' to matching ']') and discard anything after.
    json_blob = _extract_first_json_array(out)
    if not json_blob:
        print(f"  recon LLM returned no parseable JSON array: {out[:200]!r}",
              file=sys.stderr)
        return []
    try:
        moves = json.loads(json_blob)
        if not isinstance(moves, list):
            return []
        return moves
    except json.JSONDecodeError:
        print(f"  recon LLM JSON parse failed: {json_blob[:200]!r}",
              file=sys.stderr)
        return []


def _extract_first_json_array(text: str) -> str | None:
    """Find the first balanced JSON array `[...]` in text and return it.
    Counts bracket depth so embedded objects/arrays inside the top-level
    array are preserved."""
    start = text.find("[")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _emit_recon_pending(board: Path, candidates: list[dict],
                         events: list[dict], card_py: Path,
                         banner_num: int | None) -> int:
    """Write recon_pending.json for main Claude to action. Returns 0
    (recon hasn't happened yet — the file is the deliverable). Main Claude
    reads the file next turn, decides moves, calls card.py move/fly, and
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
            "moves via `card.py move <num> <col> [--writeup TEXT]` or "
            "`card.py fly <num> <col> --note TEXT`. Delete this file "
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
            "COMPLETENESS SWEEP (never miss a point — priority mandatory > notes > backlog): "
            "after emitting, re-scan EVERY digest for the categories with NO commit "
            "marker that a ship-oriented read drops — (1) urgency the user voiced "
            "('this is impt'/must/urgent/asap/p0/blocker) → a 'mandatory' card; "
            "(2) a decision/rationale/observation → a 'notes' card; (3) deferrals "
            "('later'/'next session'/'defer'/'nvm save it') → a 'backlog' card with a "
            "'⏸ OPEN — <what remains>' note. Add any that didn't already become a card; "
            "mandatory first. DELETE this file when all chunks + the sweep are done."
        ),
        "chunks": staged,
    }
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
                     banner_num: int | None = None) -> int:
    """Post-extraction LLM sweep on non-done cards. Asks LLM if any should
    move based on the activity log. Applies moves. Returns count moved.

    When CLAUDECODE=1 (we're running inside an active Claude Code session),
    skip the Haiku subprocess entirely. Main Claude already has the full
    conversation in context — write a recon_pending.json that main Claude
    actions next turn. Saves a 60-90s LLM call + tokens, and lets recon
    use the richer session context the script doesn't have."""
    try:
        with board.open("r") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0

    # Non-done, non-banner cards from columns we want to reconcile.
    candidates = [
        c for c in state.get("cards", [])
        if c.get("column") in ("task", "backlog", "inprogress", "notes")
        and "banner" not in (c.get("tags") or [])
        and "discovered" in (c.get("tags") or [])
    ]
    if not candidates:
        return 0

    # Inline-recon path: write TODO and let main Claude action it.
    if os.environ.get("CLAUDECODE") == "1":
        return _emit_recon_pending(board, candidates, events,
                                    card_py, banner_num)

    # Autonomous path: subprocess Haiku.
    print(f"▶ reconciliation sweep: {len(candidates)} non-done card(s)…",
          file=sys.stderr)
    if banner_num:
        _banner_update_text(card_py, board, banner_num,
                            f"🔍 reconciling {len(candidates)} cards…")

    moves = _llm_reconcile(candidates, events)
    if not moves:
        print("  recon: 0 moves", file=sys.stderr)
        return 0

    n_moved = 0
    for m in moves:
        num = m.get("num")
        target = m.get("target")
        reason = (m.get("reason") or "")[:160]
        if not isinstance(num, int) or target not in (
                "task", "backlog", "inprogress", "done", "mandatory"):
            continue
        # Find current column
        cur = next((c for c in candidates if c["num"] == num), None)
        if not cur:
            continue
        if cur["column"] == target:
            continue
        args = [sys.executable, str(card_py), "--board", str(board),
                "fly", str(num), target, "--pause-ms", "150"]
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
    return n_moved


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


# ---------- main driver ---------------------------------------------------

def _flatten_events(project: Path, days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)
             if days > 0 else None)
    events: list[dict] = []
    jsonl_events = harvest_jsonl(since)
    events.extend(jsonl_events)
    seen_sessions = {(e.get("meta") or {}).get("sessionId") for e in jsonl_events}
    events.extend(harvest_history(since, exclude_sessions=seen_sessions))  # #282 gap-fill
    # Convo dir is auto-derived from Claude's own data (CLAUDE.md / transcripts),
    # not hardcoded — same resolver discover2.main() uses, so the inline path is
    # as portable as the standalone tool (#284). None → convo is skipped, which
    # is fine: convo dumps are enrichment over the raw *.jsonl we already have.
    convo_dir = find_convo_dir(project)
    if convo_dir:
        events.extend(harvest_convo(since, convo_dir))
    events.extend(harvest_git(project, since))
    events.extend(harvest_memory(since))
    events.extend(harvest_plans(since))
    # Dedupe convo turns vs jsonl turns by first-80-chars text.
    seen_user: set[str] = set()
    seen_asst: set[str] = set()
    out: list[dict] = []
    for e in sorted(events, key=lambda x: x["ts"]):
        if e["kind"] in ("user_prompt", "convo_user"):
            head = (e["text"] or "").strip()[:80].lower()
            if head in seen_user:
                continue
            seen_user.add(head)
        elif e["kind"] in ("asst_msg", "convo_asst"):
            head = (e["text"] or "").strip()[:80].lower()
            if head and head in seen_asst:
                continue
            if head:
                seen_asst.add(head)
        out.append(e)
    return out


def _cwd_in_project(event: dict, project: Path) -> bool:
    cwd = (event.get("meta") or {}).get("cwd") or ""
    if not cwd:
        return True   # no cwd info = keep
    try:
        cp = Path(cwd).resolve()
        pp = project.resolve()
        return cp == pp or pp in cp.parents or cp in pp.parents
    except OSError:
        return False


def _snapshot_path(board: Path) -> Path:
    return board.parent / "extraction_snapshot.json"


def _save_snapshot(board: Path, events: list[dict], params: dict) -> None:
    """Save current board state + harvested events for offline recon testing."""
    snap_path = _snapshot_path(board)
    try:
        with board.open("r") as f:
            board_state = json.load(f)
    except (OSError, json.JSONDecodeError):
        board_state = {}
    # Serialize events with ts as ISO string so reload survives JSON.
    serializable_events = []
    for ev in events:
        ev_out = {k: v for k, v in ev.items() if k != "ts"}
        ev_out["ts"] = ev["ts"].isoformat() if hasattr(ev["ts"], "isoformat") else ev["ts"]
        serializable_events.append(ev_out)
    snapshot = {
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "params": params,
        "board": board_state,
        "events": serializable_events,
    }
    try:
        snap_path.write_text(json.dumps(snapshot, indent=2,
                                         ensure_ascii=False, default=str))
        print(f"  💾 snapshot saved → {snap_path}", file=sys.stderr)
    except OSError as e:
        print(f"  ! snapshot save failed: {e}", file=sys.stderr)


def _load_snapshot(path: Path) -> tuple[dict, list[dict]] | None:
    """Load board + events from snapshot. Returns (board_state, events)."""
    try:
        snap = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! snapshot load failed: {e}", file=sys.stderr)
        return None
    events = []
    for ev in snap.get("events", []):
        ts = ev.get("ts")
        if isinstance(ts, str):
            try:
                ev["ts"] = datetime.fromisoformat(ts.rstrip("Z") + "+00:00"
                                                   if ts.endswith("Z") else ts)
            except ValueError:
                continue
        events.append(ev)
    return snap.get("board", {}), events


def run(project: Path, board: Path, port: int, days: int,
        show_lifecycle: bool, pace_s: float,
        max_buckets: int, workers: int = 4,
        bucket_min: int = 60, chunk_size: int = 1,
        date_filter: str | None = None,
        reconcile: bool = True,
        snapshot_load: Path | None = None,
        end_days_ago: int = 0,
        recent_first: bool = False,
        mode: str = "haiku") -> None:
    card_py = Path(__file__).resolve().parent / "card.py"
    if not card_py.exists():
        print(f"card.py not found at {card_py}", file=sys.stderr)
        return

    # --snapshot-load: skip extraction, just rehydrate board + run recon.
    if snapshot_load:
        loaded = _load_snapshot(snapshot_load)
        if loaded is None:
            return
        board_state, events = loaded
        try:
            board.write_text(json.dumps(board_state, indent=2,
                                         ensure_ascii=False))
            print(f"  📂 snapshot loaded ← {snapshot_load} "
                  f"({len(board_state.get('cards', []))} cards, "
                  f"{len(events)} events)",
                  file=sys.stderr)
        except OSError as e:
            print(f"  ! board rewrite failed: {e}", file=sys.stderr)
            return
        if reconcile:
            n_moved = reconcile_sweep(card_py, board, events)
            print(f"✓ recon-only run: moved {n_moved} card(s)",
                  file=sys.stderr)
        return
    events = _flatten_events(project, days)
    if not events:
        print("no events to extract", file=sys.stderr)
        return
    # Filter to project scope: drop jsonl events whose cwd is unrelated.
    events = [e for e in events if e["kind"] != "user_prompt"
              or _cwd_in_project(e, project)]
    # Date pin: keep only events that fall on this UTC calendar day.
    if date_filter:
        try:
            target = datetime.strptime(date_filter, "%Y-%m-%d").date()
        except ValueError:
            print(f"  ! invalid --date {date_filter!r} (expected YYYY-MM-DD)",
                  file=sys.stderr)
            return
        events = [e for e in events if e["ts"].date() == target]
        if not events:
            print(f"no events on {date_filter}", file=sys.stderr)
            return
        print(f"  date filter: {date_filter} → {len(events)} events",
              file=sys.stderr)

    # Tier boundary: drop events NEWER than end_days_ago days ago, so a deep
    # backfill pass can cover only the OLDER history a quick --days 1 tier-1
    # already handled (no duplicate cards across the two passes).
    if end_days_ago and end_days_ago > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=end_days_ago)
        events = [e for e in events if e["ts"] < cutoff]
        if not events:
            print(f"no events older than {end_days_ago}d ago", file=sys.stderr)
            return
        print(f"  end-days-ago {end_days_ago}: → {len(events)} older events",
              file=sys.stderr)

    # (card_py already resolved at top of run() for snapshot-load path.)

    # Bucket by hour
    buckets: dict[int, list[dict]] = {}
    for ev in events:
        buckets.setdefault(_bucket_hour(ev["ts"], bucket_min), []).append(ev)
    # recent_first → process the newest buckets first so the most relevant
    # cards fly in immediately (cards still sort chronologically on the board
    # via their bucket-stamped createdAt; this only changes EMIT order).
    sorted_buckets = sorted(buckets.keys(), reverse=recent_first)
    if max_buckets:
        # Keep the most-recent N buckets regardless of emit order.
        sorted_buckets = (sorted_buckets[:max_buckets] if recent_first
                          else sorted_buckets[-max_buckets:])

    # Group sorted_buckets into chunks of chunk_size for batched LLM calls.
    chunks: list[list[int]] = []
    for i in range(0, len(sorted_buckets), chunk_size):
        chunks.append(sorted_buckets[i:i + chunk_size])

    print(f"▶ hourly extraction: {len(sorted_buckets)} bucket(s) of {len(events)} events "
          f"→ {len(chunks)} chunk(s) of ≤{chunk_size} bucket(s) "
          f"(mode={mode}, parallel workers={workers})",
          file=sys.stderr)

    # INLINE mode (#247, the free default): stage digests for main Claude to
    # emit — no Haiku call. The session the user is already in does the work.
    if mode == "inline":
        n = _emit_extraction_pending(board, card_py, chunks, buckets,
                                     bucket_min, project)
        print(f"✓ inline extraction staged: {n} chunk(s) await main Claude",
              file=sys.stderr)
        return

    # Progress banner: a single 'notes' card the user can watch update live.
    banner_num = _banner_create(card_py, board, len(chunks))

    from concurrent.futures import ThreadPoolExecutor, as_completed
    n_cards = 0

    def _retry_recursive_subbuckets(
            bucket_events: list[dict], current_min: int,
            depth: int = 0, max_depth: int = 3) -> list[dict]:
        """Last-resort: a single bucket of `current_min` minutes timed out.
        Re-bucket its events at half the size and retry each sub-bucket.
        Recurse until success or max_depth. Returns combined cards."""
        if not bucket_events or current_min <= 1 or depth >= max_depth:
            return []
        half_min = max(1, current_min // 2)
        sub_buckets: dict[int, list[dict]] = {}
        for ev in bucket_events:
            sub_buckets.setdefault(
                _bucket_hour(ev["ts"], half_min), []).append(ev)
        sub_keys = sorted(sub_buckets.keys())
        print(f"  ↻↻ recursive retry depth={depth+1}: "
              f"re-bucket {len(bucket_events)} events at {half_min}min "
              f"→ {len(sub_keys)} sub-bucket(s)", file=sys.stderr)
        recovered: list[dict] = []
        for sk in sub_keys:
            label = _bucket_label(sk, half_min)
            cards = extract_cards_for_chunk(
                [(label, sub_buckets[sk])], project)
            if not cards:
                cards = _retry_recursive_subbuckets(
                    sub_buckets[sk], half_min, depth + 1, max_depth)
            recovered.extend(cards)
        return recovered

    def _do_chunk(chunk_keys: list[int]) -> tuple[list[int], list[dict]]:
        chunk = [(_bucket_label(k, bucket_min), buckets[k])
                 for k in chunk_keys]
        cards = extract_cards_for_chunk(chunk, project)
        # Tier 1 retry: chunk-size > 1 → split in half (smaller LLM digests).
        if not cards and len(chunk_keys) > 1:
            mid = len(chunk_keys) // 2
            halves = [chunk_keys[:mid], chunk_keys[mid:]]
            print(f"  ↻ retry: splitting failed chunk "
                  f"[{', '.join(_bucket_label(k, bucket_min) for k in chunk_keys)}] "
                  f"into {len(halves)} halves",
                  file=sys.stderr)
            recovered: list[dict] = []
            for half in halves:
                sub_chunk = [(_bucket_label(k, bucket_min), buckets[k])
                             for k in half]
                sub_cards = extract_cards_for_chunk(sub_chunk, project)
                if not sub_cards and len(half) == 1:
                    # Tier 2 retry: single-bucket call STILL failed → recursive
                    # sub-bucketing at half bucket_min.
                    sub_cards = _retry_recursive_subbuckets(
                        buckets[half[0]], bucket_min)
                recovered.extend(sub_cards)
            cards = recovered
        elif not cards and len(chunk_keys) == 1:
            # Tier 2 retry from the start (chunk-size 1 input that failed).
            cards = _retry_recursive_subbuckets(
                buckets[chunk_keys[0]], bucket_min)
        return chunk_keys, cards

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_do_chunk, c): c for c in chunks}
        completed = 0
        # Emit cards as chunks finish (no chronological ordering — per user
        # 5/28: 'dont worry about rearranging, we can arrange by time later').
        for fut in as_completed(futures):
            try:
                chunk_keys, cards = fut.result()
            except Exception as e:
                chunk_keys = futures[fut]
                cards = []
                print(f"  ! chunk error: {e}", file=sys.stderr)
            completed += 1
            label_summary = " + ".join(_bucket_label(k, bucket_min)
                                       for k in chunk_keys)
            print(f"  [{completed}/{len(chunks)}] [{label_summary}]  "
                  f"→ {len(cards)} card(s) extracted",
                  file=sys.stderr)
            # Bucket ts for createdAt = first bucket's start (ISO).
            first_bucket_ts = datetime.fromtimestamp(
                chunk_keys[0] * bucket_min * 60, tz=timezone.utc).isoformat()
            for card in cards:
                card["_bucket_label"] = label_summary
                card["_bucket_ts_iso"] = first_bucket_ts
                num = emit_card(card_py, board, card,
                                show_lifecycle, pace_s)
                if num:
                    n_cards += 1
                time.sleep(pace_s)
            # Update the banner after each chunk completes.
            if banner_num:
                _banner_update(card_py, board, banner_num,
                               completed, len(chunks), n_cards)

    # Save snapshot of post-extraction state BEFORE reconciliation, so
    # offline recon testing can iterate against a stable baseline.
    _save_snapshot(board, events, {
        "project": str(project), "days": days, "bucket_min": bucket_min,
        "chunk_size": chunk_size, "date_filter": date_filter,
        "n_buckets": len(sorted_buckets), "n_chunks": len(chunks),
        "n_cards": n_cards,
    })

    # Reconciliation sweep — catches "user said nvm" / "matching commit"
    # signals that the per-bucket extraction missed.
    n_moved = 0
    if reconcile:
        n_moved = reconcile_sweep(card_py, board, events, banner_num)

    # Banner → done at the end.
    if banner_num:
        _banner_finish(card_py, board, banner_num, n_cards,
                       len(sorted_buckets), len(chunks), n_moved)

    print(f"✓ emitted {n_cards} card(s) across {len(sorted_buckets)} bucket(s) "
          f"in {len(chunks)} chunk(s); recon moved {n_moved} card(s)",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--board", type=Path, required=True)
    ap.add_argument("--port", type=int, default=7894)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--max-buckets", type=int, default=0,
                    help="cap N most-recent hourly buckets (0 = all)")
    ap.add_argument("--show-lifecycle", action="store_true",
                    help="play task→ip→done flight per card (slower, more theatre)")
    ap.add_argument("--pace", type=float, default=0.3,
                    help="seconds between card-add operations")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel LLM workers (default 4)")
    ap.add_argument("--bucket-min", type=int, default=60,
                    help="bucket size in minutes (default 60)")
    ap.add_argument("--chunk-size", type=int, default=1,
                    help="buckets per LLM call (default 1 = no batching; "
                         "set 2-4 to amortize claude -p cold-start)")
    ap.add_argument("--date", type=str, default=None,
                    help="YYYY-MM-DD UTC — restrict to events on this day only")
    ap.add_argument("--no-reconcile", action="store_true",
                    help="skip the post-extraction reconciliation sweep")
    ap.add_argument("--snapshot-load", type=Path, default=None,
                    help="path to extraction_snapshot.json — skips extraction "
                         "and runs only the reconciliation sweep against the "
                         "saved state. Lets us iterate on recon LLM prompts "
                         "without paying ~10min per test.")
    ap.add_argument("--end-days-ago", type=int, default=0,
                    help="only include events OLDER than N days ago (tier-2 "
                         "backfill boundary; 0 = no cutoff)")
    ap.add_argument("--recent-first", action="store_true",
                    help="emit newest buckets first so the most relevant cards "
                         "fly in immediately (board still sorts chronologically)")
    ap.add_argument("--mode", choices=["haiku", "inline"], default="haiku",
                    help="haiku = claude -p per chunk (costs usage); "
                         "inline = stage extraction_pending.json for main Claude "
                         "to emit (free, no Haiku, higher quality)")
    args = ap.parse_args()
    os.environ["BOARD_SERVER"] = f"http://127.0.0.1:{args.port}"
    run(args.project.resolve(), args.board.resolve(), args.port,
        args.days, args.show_lifecycle, args.pace, args.max_buckets,
        workers=args.workers, bucket_min=args.bucket_min,
        chunk_size=args.chunk_size, date_filter=args.date,
        reconcile=not args.no_reconcile,
        snapshot_load=args.snapshot_load,
        end_days_ago=args.end_days_ago,
        recent_first=args.recent_first,
        mode=args.mode)


if __name__ == "__main__":
    main()
