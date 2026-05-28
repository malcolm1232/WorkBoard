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
  "title": "verb + noun phrase, ≤70 chars. Examples: 'BOARD-FLY: atomic-hop primitive', 'Fix card-drag freeze on iPhone', 'Investigate convo dedup'. NO conversational openers (btw, can u, oh wait). NO verbatim user wording — summarize the WORK.",
  "code": "short kebab or CAPS code from noun cluster, ≤24 chars (e.g. 'BOARD-FLY', 'DISCOVER2'). Empty string if not a build/feature card.",
  "column": "one of: task | backlog | inprogress | done | mandatory | notes",
  "priority": "low | mid | critical",
  "notes": "~2 sentences problem statement + fix direction or context. ≤200 chars. Empty string if no signal.",
  "tags": ["one or two from: feature | bug | fix | refactor | doc | design | discipline | infrastructure"]
}

Column routing rules:
- "done"       → a git commit landed in this hour OR a clean ship phrase appeared (shipped X / deployed / merged)
- "mandatory"  → user said urgent / must / impt / critical / asap / blocker / 'this is impt'
- "inprogress" → files were edited but no ship hit
- "task"       → mentioned, named, planned but no edits yet
- "backlog"    → deferred ("later", "next session", "tomorrow")
- "notes"      → captured observation / idea / decision, NOT a unit of work to ship

Quality bar:
- Skip conversational micro-turns ("yes", "ok", "stop", "open the board", "rerun"). They are NOT cards.
- One unit of work = one card. If the user asked about feature X, you built it, and they reviewed it — that is ONE card titled by what X is.
- If two units of work happened in the same hour, return two cards.
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

def _card_add(card_py: Path, board: Path, card: dict) -> int | None:
    title = (card.get("title") or "").strip()[:80]
    if not title:
        return None
    code = (card.get("code") or "").strip()
    if code and not title.lower().startswith(code.lower()):
        title = f"{code}: {title}"[:80]
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


def run(project: Path, board: Path, port: int, days: int,
        show_lifecycle: bool, pace_s: float,
        max_buckets: int, workers: int = 4,
        bucket_min: int = 60, chunk_size: int = 1) -> None:
    events = _flatten_events(project, days)
    if not events:
        print("no events to extract", file=sys.stderr)
        return
    # Filter to project scope: drop jsonl events whose cwd is unrelated.
    events = [e for e in events if e["kind"] != "user_prompt"
              or _cwd_in_project(e, project)]

    card_py = Path(__file__).resolve().parent / "card.py"
    if not card_py.exists():
        print(f"card.py not found at {card_py}", file=sys.stderr)
        return

    # Bucket by hour
    buckets: dict[int, list[dict]] = {}
    for ev in events:
        buckets.setdefault(_bucket_hour(ev["ts"], bucket_min), []).append(ev)
    sorted_buckets = sorted(buckets.keys())
    if max_buckets:
        sorted_buckets = sorted_buckets[-max_buckets:]   # most-recent N hours

    # Group sorted_buckets into chunks of chunk_size for batched LLM calls.
    chunks: list[list[int]] = []
    for i in range(0, len(sorted_buckets), chunk_size):
        chunks.append(sorted_buckets[i:i + chunk_size])

    print(f"▶ hourly extraction: {len(sorted_buckets)} bucket(s) of {len(events)} events "
          f"→ {len(chunks)} chunk(s) of ≤{chunk_size} bucket(s) "
          f"(parallel workers={workers})",
          file=sys.stderr)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    n_cards = 0

    def _do_chunk(chunk_keys: list[int]) -> tuple[list[int], list[dict]]:
        chunk = [(_bucket_label(k, bucket_min), buckets[k])
                 for k in chunk_keys]
        cards = extract_cards_for_chunk(chunk, project)
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
            for card in cards:
                card["_bucket_label"] = label_summary
                num = emit_card(card_py, board, card,
                                show_lifecycle, pace_s)
                if num:
                    n_cards += 1
                time.sleep(pace_s)

    print(f"✓ emitted {n_cards} card(s) across {len(sorted_buckets)} bucket(s) "
          f"in {len(chunks)} chunk(s)",
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
    args = ap.parse_args()
    os.environ["BOARD_SERVER"] = f"http://127.0.0.1:{args.port}"
    run(args.project.resolve(), args.board.resolve(), args.port,
        args.days, args.show_lifecycle, args.pace, args.max_buckets,
        workers=args.workers, bucket_min=args.bucket_min,
        chunk_size=args.chunk_size)


if __name__ == "__main__":
    main()
