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
    parse_ts,
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
  "tags": ["one or two from: feature | bug | fix | refactor | doc | design | discipline | infrastructure"]
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


def build_digest(bucket_events: list[dict], project: Path) -> str:
    """Compact chronological digest of an hour of events for the LLM."""
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
                lines.append(f"  [{ts}] CLAUDE: {head}")
        elif kind == "git_commit":
            sha = (ev.get("meta") or {}).get("shaShort", "")
            lines.append(f"  [{ts}] COMMIT {sha}: {ev['text'][:120]}")
        elif kind == "memory_write":
            lines.append(f"  [{ts}] MEMORY: {ev['text']}")
        elif kind == "plan_write":
            lines.append(f"  [{ts}] PLAN: {ev['text']}")
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
    for label, bevents in chunk:
        digest = build_digest(bevents, project)
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
              writeup: str | None = None) -> bool:
    args = [sys.executable, str(card_py), "--board", str(board), "fly",
            str(num), col, "--pause-ms", "150"]
    if writeup:
        args += ["--writeup", writeup[:200]]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=8)
    except subprocess.SubprocessError:
        return False
    return out.returncode == 0


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
    events.extend(harvest_jsonl(since))
    events.extend(harvest_convo(since))
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
        recent_first: bool = False) -> None:
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
          f"(parallel workers={workers})",
          file=sys.stderr)

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
    args = ap.parse_args()
    os.environ["BOARD_SERVER"] = f"http://127.0.0.1:{args.port}"
    run(args.project.resolve(), args.board.resolve(), args.port,
        args.days, args.show_lifecycle, args.pace, args.max_buckets,
        workers=args.workers, bucket_min=args.bucket_min,
        chunk_size=args.chunk_size, date_filter=args.date,
        reconcile=not args.no_reconcile,
        snapshot_load=args.snapshot_load,
        end_days_ago=args.end_days_ago,
        recent_first=args.recent_first)


if __name__ == "__main__":
    main()
