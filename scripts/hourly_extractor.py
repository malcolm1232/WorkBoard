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


# Canonical harvest sources (the `source` field every event carries, set in
# discover2.harvest_*). The single identifiable list of what the token budget is
# spent on — pass a subset via --sources to TARGET specific streams:
#   jsonl   — *.jsonl transcripts (the spine; user prompts + assistant turns)
#   history — conversation_raw_*.md summaries (#282 gap-fill for sessions not in jsonl)
#   convo   — conversation_verbatim_*.md dumps (enrichment over jsonl)
#   git     — git commit log
#   memory  — ~/.claude .../memory/*.md writes
#   plans   — plan-file writes
SOURCES = ("jsonl", "history", "convo", "git", "memory", "plans")




# Extracted helpers (#307 file-split). Acyclic: reconcile→{common,emit}.
from hourly_common import *     # noqa: E402,F401,F403  _CLAUDE_BIN/_LLM_MODEL/_bucket_*/build_digest
from hourly_emit import *       # noqa: E402,F401,F403  _banner_*/_card_*/emit_card
from hourly_reconcile import *  # noqa: E402,F401,F403  reconcile_sweep/_emit_extraction_pending/...


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
            env=_LLM_ENV,   # MAX_THINKING_TOKENS=0 — the haiku-fill bottleneck fix
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"  ! LLM call failed for chunk [{label_summary}]: {e}",
              file=sys.stderr)
        return []
    if proc.returncode != 0:
        print(f"  ! claude -p exit {proc.returncode} for chunk [{label_summary}]",
              file=sys.stderr)
        return []
    cards = parse_card_array(proc.stdout)
    if cards is None:
        print(f"  ! LLM returned non-JSON for chunk [{label_summary}]",
              file=sys.stderr)
        return []
    return cards




# ---------- main driver ---------------------------------------------------

def _flatten_events(project: Path, days: int,
                    sources: set | None = None) -> list[dict]:
    """Harvest + merge all event streams. Pass `sources` (a subset of SOURCES)
    to TARGET specific streams — excluded harvests are skipped entirely, not
    just filtered after, so targeting also saves the harvest cost."""
    def want(src: str) -> bool:
        return sources is None or src in sources
    since = (datetime.now(timezone.utc) - timedelta(days=days)
             if days > 0 else None)
    events: list[dict] = []
    seen_sessions: set = set()
    if want("jsonl"):
        jsonl_events = harvest_jsonl(since)
        events.extend(jsonl_events)
        seen_sessions = {(e.get("meta") or {}).get("sessionId") for e in jsonl_events}
    if want("history"):
        events.extend(harvest_history(since, exclude_sessions=seen_sessions))  # #282 gap-fill
    # Convo dir is auto-derived from Claude's own data (CLAUDE.md / transcripts),
    # not hardcoded — same resolver discover2.main() uses, so the inline path is
    # as portable as the standalone tool (#284). None → convo is skipped, which
    # is fine: convo dumps are enrichment over the raw *.jsonl we already have.
    if want("convo"):
        convo_dir = find_convo_dir(project)
        if convo_dir:
            events.extend(harvest_convo(since, convo_dir))
    if want("git"):
        events.extend(harvest_git(project, since))
    if want("memory"):
        events.extend(harvest_memory(since))
    if want("plans"):
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


def _run_snapshot_load(board: Path, card_py: Path,
                       snapshot_load: Path, reconcile: bool) -> None:
    """--snapshot-load: skip extraction, just rehydrate board + run recon."""
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


def _filter_events(events: list[dict], project: Path,
                   date_filter: str | None,
                   end_days_ago: int,
                   seed_if_empty: bool = False) -> list[dict] | None:
    """Apply project-scope, date-pin and tier-boundary filters in order.
    Returns the filtered events, or None if a filter emptied them (the
    caller then returns — matching the original early-return behavior)."""
    # Filter to project scope: drop jsonl events whose cwd is unrelated.
    scoped = [e for e in events if e["kind"] != "user_prompt"
              or _cwd_in_project(e, project)]
    # #285 never-empty first-run seed: a brand-new repo has ZERO in-project
    # user prompts, so project-scoping strips all conversation signal and the
    # board comes up blank on day one — silently breaking VISION's "see your
    # last week of work" promise for every new adopter. When asked to seed
    # (bootstrap only), detect that exact case — we HAD prompts but kept none
    # in-project — and fall back to the UNFILTERED cross-project history for
    # this first fill, loudly (VISION §4: no silent caps). Existing boards keep
    # in-project prompts so this never fires for them; going-forward capture
    # stays project-scoped because the cron/hourly pass runs without this flag.
    if seed_if_empty:
        had_prompts = any(e["kind"] == "user_prompt" for e in events)
        kept_prompts = any(e["kind"] == "user_prompt" for e in scoped)
        if had_prompts and not kept_prompts:
            print("⚠ #285 seed: no Claude history in this project yet — "
                  "seeding the board from your recent CROSS-PROJECT history so "
                  "day one isn't blank. Going-forward cards stay project-scoped.",
                  file=sys.stderr)
            scoped = events  # cross-project seed (bounded by --days window)
    events = scoped
    # Date pin: keep only events that fall on this UTC calendar day.
    if date_filter:
        try:
            target = datetime.strptime(date_filter, "%Y-%m-%d").date()
        except ValueError:
            print(f"  ! invalid --date {date_filter!r} (expected YYYY-MM-DD)",
                  file=sys.stderr)
            return None
        events = [e for e in events if e["ts"].date() == target]
        if not events:
            print(f"no events on {date_filter}", file=sys.stderr)
            return None
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
            return None
        print(f"  end-days-ago {end_days_ago}: → {len(events)} older events",
              file=sys.stderr)
    return events


def _bucketize(events: list[dict], bucket_min: int, recent_first: bool,
               max_buckets: int, chunk_size: int
               ) -> tuple[list[int], dict[int, list[dict]], list[list[int]]]:
    """Group events into hour buckets, order them, and batch into chunks.
    Returns (sorted_bucket_keys, buckets_by_key, chunks_of_keys)."""
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
    return sorted_buckets, buckets, chunks


def _extract_chunk_with_retries(chunk_keys: list[int],
                                buckets: dict[int, list[dict]],
                                project: Path,
                                bucket_min: int) -> list[dict]:
    """Extract one chunk's cards with the two-tier retry ladder on failure:
    tier 1 splits a multi-bucket chunk in half; tier 2 recursively re-buckets
    a single failed bucket at half bucket_min."""

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
    return cards


def _extract_haiku(project: Path, board: Path, card_py: Path,
                   buckets: dict[int, list[dict]], chunks: list[list[int]],
                   sorted_buckets: list[int], events: list[dict],
                   bucket_min: int, workers: int, chunk_size: int,
                   days: int, date_filter: str | None,
                   show_lifecycle: bool, pace_s: float,
                   reconcile: bool) -> None:
    """HAIKU mode: parallel per-chunk extraction → emit cards as chunks finish,
    snapshot the result, then reconcile. The autonomous (costs-Haiku) path."""
    # Progress banner: a single 'notes' card the user can watch update live.
    banner_num = _banner_create(card_py, board, len(chunks))

    from concurrent.futures import ThreadPoolExecutor, as_completed
    n_cards = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_extract_chunk_with_retries, c, buckets,
                        project, bucket_min): c
            for c in chunks
        }
        completed = 0
        # Emit cards as chunks finish (no chronological ordering — per user
        # 5/28: 'dont worry about rearranging, we can arrange by time later').
        for fut in as_completed(futures):
            chunk_keys = futures[fut]
            try:
                cards = fut.result()
            except Exception as e:
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


def run(project: Path, board: Path, port: int, days: int,
        show_lifecycle: bool, pace_s: float,
        max_buckets: int, workers: int = 4,
        bucket_min: int = 60, chunk_size: int = 1,
        date_filter: str | None = None,
        reconcile: bool = True,
        snapshot_load: Path | None = None,
        end_days_ago: int = 0,
        recent_first: bool = False,
        mode: str = "haiku",
        sources: set | None = None,
        seed_if_empty: bool = False) -> None:
    card_py = Path(__file__).resolve().parent / "card.py"
    if not card_py.exists():
        print(f"card.py not found at {card_py}", file=sys.stderr)
        return

    # --snapshot-load: skip extraction, just rehydrate board + run recon.
    if snapshot_load:
        _run_snapshot_load(board, card_py, snapshot_load, reconcile)
        return

    events = _flatten_events(project, days, sources=sources)
    if not events:
        print("no events to extract", file=sys.stderr)
        return
    events = _filter_events(events, project, date_filter, end_days_ago,
                            seed_if_empty=seed_if_empty)
    if events is None:
        return

    sorted_buckets, buckets, chunks = _bucketize(
        events, bucket_min, recent_first, max_buckets, chunk_size)

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

    _extract_haiku(project, board, card_py, buckets, chunks, sorted_buckets,
                   events, bucket_min, workers, chunk_size, days, date_filter,
                   show_lifecycle, pace_s, reconcile)


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
    ap.add_argument("--sources", type=str, default=None,
                    help="comma-separated subset of "
                         + ",".join(SOURCES) +
                         " to TARGET (default: all). Excluded harvests are "
                         "skipped entirely. e.g. --sources jsonl,git")
    ap.add_argument("--mode", choices=["haiku", "inline"], default="haiku",
                    help="haiku = claude -p per chunk (costs usage); "
                         "inline = stage extraction_pending.json for main Claude "
                         "to emit (free, no Haiku, higher quality)")
    ap.add_argument("--seed-cross-project-if-empty", action="store_true",
                    help="#285: if this project has NO local history (fresh "
                         "adopter), seed the first fill from recent cross-project "
                         "history so the board isn't blank on day one. Off by "
                         "default (strict project scope); bootstrap turns it on.")
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
        mode=args.mode,
        sources=({s.strip() for s in args.sources.split(",") if s.strip()}
                 if args.sources else None),
        seed_if_empty=args.seed_cross_project_if_empty)


if __name__ == "__main__":
    main()
