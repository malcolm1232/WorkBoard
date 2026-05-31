#!/usr/bin/env python3
"""hourly_extractor digest primitives — extracted from hourly_extractor.py (#307 file-split).

The LLM-call config constants + the event→text digest builder. Shared by the
LLM dispatch (hourly_extractor) AND the reconciliation sweep (hourly_reconcile),
so it lives in this leaf module to keep the dependency graph acyclic.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
_LLM_MODEL = os.environ.get("HOURLY_MODEL", "haiku")

# Claude Code runs the model with EXTENDED THINKING on by default — for this
# stateless digest→JSON extraction that burns ~5k reasoning tokens/call (stripped
# from the output, so invisible) and dominates latency (~50s/call). Forcing
# MAX_THINKING_TOKENS=0 cuts output_tokens ~13× (5220→392) and api time ~15×
# (53s→3.5s) with cards fully intact — it was THE haiku-fill bottleneck (measured
# 2026-05-31, not card verbosity/MCP/chunk-size/parallelism). Every claude -p
# call below MUST use this env. Override-respecting: honors a caller's own value.
_LLM_ENV = {**os.environ}
_LLM_ENV.setdefault("MAX_THINKING_TOKENS", "0")


# ---------- digest builder ------------------------------------------------

def _bucket_hour(ts: datetime, bucket_min: int = 60) -> int:
    return int(ts.timestamp()) // (bucket_min * 60)


def _bucket_label(bucket: int, bucket_min: int = 60) -> str:
    dt = datetime.fromtimestamp(bucket * bucket_min * 60, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# #299 DIGEST-COMPACT: the lossless token-cut layer lives in its own module
# (scripts/digest_compact.py) so all future token work has ONE identifiable home
# and the logic is exportable on its own. build_digest just assembles the raw
# lines; digest_compact.compact() drops the zero-signal boilerplate.
import digest_compact


def build_digest(bucket_events: list[dict], project: Path,
                 seen_heads: set | None = None) -> str:
    """Chronological digest of an hour of events for the LLM. Assembles raw
    lines, then hands them to digest_compact.compact() for the lossless cut.
    Pass a shared `seen_heads` set across buckets/chunks to dedup repeated
    non-signal heads end-to-end."""
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
    return "\n".join(digest_compact.compact(lines, seen_heads))



# The LLM extraction prompt — shared by hourly_extractor's dispatch AND
# hourly_reconcile's _emit_extraction_pending (card_format), so it lives here.
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


__all__ = ["_CLAUDE_BIN", "_LLM_MODEL", "_LLM_ENV", "_LLM_PROMPT", "_bucket_hour", "_bucket_label", "build_digest"]
