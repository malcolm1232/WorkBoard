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
import tempfile
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




# Extracted helpers (#307/#646 file-split). Acyclic: reconcile→{common,emit};
# extract_llm→common. No leaf imports back into hourly_extractor.
from hourly_common import *      # noqa: E402,F401,F403  _CLAUDE_BIN/_LLM_MODEL/_bucket_*/build_digest
from hourly_emit import *        # noqa: E402,F401,F403  _banner_*/_card_*/emit_card
from hourly_reconcile import *   # noqa: E402,F401,F403  reconcile_sweep/_emit_extraction_pending/...
from hourly_extract_llm import (  # noqa: E402  #646: LLM dispatch + retry ladder leaf
    ChunkExtractionError, extract_cards_for_hour, extract_cards_for_chunk,
    _extract_chunk_with_retries,
)


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


# Ancestors of (nearly) every project — $HOME, the users-root, ~/Desktop, the FS
# root. A session run from one of these must NOT count as "in" a project below
# it, or every home session leaks into every board (the Edu-on-WorkBoard bug
# #508). #530: Path.home().parent is the PORTABLE users-root (/Users on macOS,
# /home on Linux, C:\Users on Windows) — never hardcode the macOS /Users alone.
_BROAD_ROOTS = {Path.home(), Path.home().parent, Path.home() / "Desktop", Path("/")}


def _cwd_in_project(event: dict, project: Path) -> bool:
    cwd = (event.get("meta") or {}).get("cwd") or ""
    if not cwd:
        return True   # no cwd info = keep (memory/plans/convo carry none)
    try:
        cp = Path(cwd).resolve()
        pp = project.resolve()
    except OSError:
        return False
    if cp == pp:
        return True
    if pp in cp.parents:
        return True   # cwd is a subdir of the project (e.g. WorkBoard/scripts)
    # cwd is an ANCESTOR of the project (session run from a parent dir): keep
    # only when that parent is itself a real project, never a broad root —
    # otherwise $HOME/Desktop leak into every board (#508).
    if cp in pp.parents and cp not in _BROAD_ROOTS:
        return True
    return False


def _event_in_project(event: dict, project: Path) -> bool:
    """Does this turn's WORK belong to `project`? (#508 follow-up.)

    Attribute by what the turn actually CHANGED, not just where it ran: cwd is
    frequently $HOME while editing another project's files by absolute path
    (e.g. a home session editing WorkBoard/templates/board.html), so cwd alone
    both leaks other projects' work AND drops in-project work. Files touched are
    the decisive signal; cwd is only the fallback when a turn edited nothing
    (user prompts, memory/plans/convo events carry no files).

    - edited a file under the project → belongs here (True), whatever the cwd
    - edited ONLY other projects' files → not here (False)
    - no decisive file signal → fall back to cwd scoping
    This is what lets one conversation that hops between projects split cleanly.
    """
    files = event.get("files") or []
    if files:
        try:
            pp = project.resolve()
        except OSError:
            pp = project
        touched_here = touched_other = False
        for f in files:
            try:
                fp = Path(f).resolve()
            except OSError:
                continue
            if fp == pp or pp in fp.parents:
                touched_here = True
            else:
                touched_other = True
        if touched_here:
            return True
        if touched_other:
            return False
        # files present but none resolved to a real path → fall through to cwd
    return _cwd_in_project(event, project)


def _last_activity_ms(project: Path) -> int:
    """Newest ~/.claude/history.jsonl timestamp (ms) for this project, or 0.

    history.jsonl is the cheap all-projects prompt chronicle: one record per
    typed prompt with {project=cwd, timestamp=ms}. We take the newest record
    whose cwd matches this project (same nesting rule as _cwd_in_project)."""
    hist = Path.home() / ".claude" / "history.jsonl"
    if not hist.exists():
        return 0
    try:
        pp = project.resolve()
    except OSError:
        return 0
    newest_ms = 0
    try:
        with hist.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                cwd = o.get("project") or ""
                raw = o.get("timestamp")
                if not cwd or not raw:
                    continue
                try:
                    cp = Path(cwd).resolve()
                except OSError:
                    continue
                if not (cp == pp or pp in cp.parents or cp in pp.parents):
                    continue
                try:
                    ms = int(raw)
                except (TypeError, ValueError):
                    continue
                if ms > newest_ms:
                    newest_ms = ms
    except OSError:
        return 0
    return newest_ms


def _anchor_offset_days(project: Path) -> int:
    """Days between now and the project's LAST session (0 = worked today).

    The fly-in window must END at the last session, not at `now` — otherwise an
    idle gap (e.g. didn't work for 2 days) empties a 2-day window and nothing
    flies in. Returns 0 (anchor = now = legacy behavior) if history is
    missing/pruned for this project."""
    newest_ms = _last_activity_ms(project)
    if not newest_ms:
        return 0
    last = datetime.fromtimestamp(newest_ms / 1000.0, tz=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - last).days)


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


# ---------- SessionStart smart-recon (--reconcile-only) ----------

def _recon_state_path(board: Path) -> Path:
    return board.parent / ".recon_state.json"


def _read_last_recon_ms(board: Path) -> int:
    """Epoch-ms of the last reconcile run for this board, or 0 if never."""
    try:
        raw = json.loads(_recon_state_path(board).read_text()).get("last_recon_ms")
        return int(raw) if raw else 0
    except Exception:
        return 0


def _write_last_recon_ms(board: Path, ms: int) -> None:
    try:
        _recon_state_path(board).write_text(json.dumps({"last_recon_ms": int(ms)}))
    except OSError:
        pass


# ---------- Card-replay gate (#recon-after-replay) ----------
# The single source of truth for "is the bootstrap fly-in of the past N days
# still streaming in?". Reconcile must NOT fire while a replay is in progress —
# a recon pass racing the fill is what made cards "jump all over the place"
# (two reconciles + live emits hitting the board at once). All reconcile entry
# points gate on `completed_card_replay`: the end-of-replay sweep runs exactly
# ONCE, the moment the LAST tier finishes; the SessionStart recon-only pass
# stands down entirely while a replay is live. Default-open: a board that never
# went through a tier-fly bootstrap (no state file) reconciles as before.

def _replay_state_path(board: Path) -> Path:
    return board.parent / ".replay_state.json"


def _write_replay_state(p: Path, state: dict) -> bool:
    """Atomically write the replay-state file (tempfile + os.replace, so a
    crash mid-write never leaves a torn JSON). Returns False on OSError — the
    caller decides the fail-safe DIRECTION (#642), since the two gate writes
    fail safely in opposite directions."""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".replay_",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(state))
            os.replace(tmp, p)
        finally:
            try:
                os.unlink(tmp)   # no-op after a successful replace
            except OSError:
                pass
        return True
    except OSError:
        return False


def _mark_replay_started(board: Path, n_days: int) -> None:
    """Open the gate's 'in progress' state: completed_card_replay = 0. Called
    at the top of the tier-fly bootstrap, before any card flies in.

    #642: if the write fails, the gate file simply isn't created → _replay_complete
    defaults OPEN → recon just isn't gated during this fill (the milder, pre-gate
    behavior). That's the safe direction here — never a permanent block."""
    if not _write_replay_state(_replay_state_path(board), {
        "completed_card_replay": 0,
        "n_days": int(n_days),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }):
        print("  ! could not write replay-state at start; recon gate disabled "
              "for this fill (#642)", file=sys.stderr)


def _mark_replay_complete(board: Path,
                          failed_buckets: list[int] | None = None) -> None:
    """Flip completed_card_replay → 1 once the LAST replay tier has finished
    emitting. Only after this does reconcile run.

    #627: `failed_buckets` records buckets that hard-failed extraction. The gate
    still flips to 1 (so recon is never stuck — #384) but the fill is stamped
    `partial: true` with the dropped bucket keys, turning a silent drop into a
    recorded, re-runnable one."""
    p = _replay_state_path(board)
    try:
        state = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    state["completed_card_replay"] = 1
    state["completed_at"] = datetime.now(timezone.utc).isoformat()
    state["partial"] = bool(failed_buckets)
    state["failed_buckets"] = list(failed_buckets or [])
    if not _write_replay_state(p, state):
        # #642: leaving completed_card_replay=0 on disk would PERMANENTLY disable
        # recon for this board — every future SessionStart recon would stand down
        # forever (the gate never reopens). Fail OPEN: remove the stale state file
        # so _replay_complete defaults to True. A re-bootstrap can re-stamp it.
        print("  ! could not write replay-state at complete; failing gate OPEN "
              "to avoid permanently-stuck recon (#642)", file=sys.stderr)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _replay_complete(board: Path) -> bool:
    """True when reconcile is allowed: either no replay is tracked for this
    board (default-open) or the tracked replay has finished. False ONLY while a
    replay is actively streaming (state file exists with the flag still 0)."""
    p = _replay_state_path(board)
    if not p.exists():
        return True
    try:
        return bool(json.loads(p.read_text()).get("completed_card_replay", 1))
    except (OSError, json.JSONDecodeError):
        return True


def _has_nondone_cards(board: Path) -> bool:
    """True if any non-banner card sits in a reconcilable (non-done) column."""
    try:
        state = json.loads(board.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return any(
        c.get("column") in ("task", "backlog", "inprogress", "notes")
        and "banner" not in (c.get("tags") or [])
        for c in state.get("cards", [])
    )


def _run_reconcile_only(project: Path, board: Path) -> None:
    """SessionStart smart-recon: reconcile EVERY non-done card against activity
    since the last recon. Two cheap gates keep it token-frugal for free users —
    no Haiku call unless there are non-done cards AND new activity since the last
    run. Reconcile-only (only_discovered=False) — no card creation here; net-new
    un-carded work is the Stop hook's job."""
    card_py = Path(__file__).resolve().parent / "card.py"
    if not card_py.exists():
        return

    # Gate 0 (#recon-after-replay): a bootstrap fly-in is still streaming the
    # past-N-days card replay → stand down. Reconciling now would race the fill's
    # live emits (the "cards jumped all over the place" report). The replay's own
    # end-of-replay sweep covers this window; this pass resumes next session once
    # completed_card_replay == 1.
    if not _replay_complete(board):
        print("recon-only: card replay in progress — skip", file=sys.stderr)
        return

    # Gate A: nothing to reconcile → no harvest, no Haiku.
    if not _has_nondone_cards(board):
        print("recon-only: no non-done cards — skip", file=sys.stderr)
        return

    # Gate B: gate on PROJECT-SCOPED activity (history.jsonl, the same oracle
    # _anchor_offset_days trusts). No recorded activity for this project → nothing
    # to reconcile against; activity older than the last recon → nothing new.
    # Either way, skip before harvesting/Haiku. (Harvest keeps no-cwd global
    # events like memory/plans, so we can't rely on harvest-emptiness here.)
    last_ms = _read_last_recon_ms(board)
    activity_ms = _last_activity_ms(project)
    if not activity_ms:
        print("recon-only: no recorded project activity — skip", file=sys.stderr)
        return
    if last_ms and activity_ms <= last_ms:
        print("recon-only: no new activity since last recon — skip",
              file=sys.stderr)
        return

    # Window = since the last recon (first run → last 2 days), capped at 14d so a
    # long-idle project doesn't harvest forever.
    now = datetime.now(timezone.utc)
    if last_ms:
        span_days = (now - datetime.fromtimestamp(last_ms / 1000.0,
                                                   tz=timezone.utc)).days
        days = max(1, min(span_days + 1, 14))
    else:
        days = 2

    events = _flatten_events(project, days,
                             sources={"jsonl", "history", "convo", "git"})
    # Scope to THIS project (harvest is global) — reconcile a board only against
    # its own activity, never another project's. Mirrors _run_window.
    events = _filter_events(events, project, None, 0) or []
    if not events:
        print("recon-only: no activity in window — skip", file=sys.stderr)
        # Still stamp the marker so we don't re-harvest the same empty window.
        _write_last_recon_ms(board, int(now.timestamp() * 1000))
        return

    n = reconcile_sweep(card_py, board, events, only_discovered=False)
    print(f"✓ recon-only: moved {n} card(s)", file=sys.stderr)
    _write_last_recon_ms(board, int(now.timestamp() * 1000))


def _filter_events(events: list[dict], project: Path,
                   date_filter: str | None,
                   end_days_ago: int,
                   seed_if_empty: bool = False) -> list[dict] | None:
    """Apply project-scope, date-pin and tier-boundary filters in order.
    Returns the filtered events, or None if a filter emptied them (the
    caller then returns — matching the original early-return behavior)."""
    # Filter to project scope: drop ANY cwd-bearing event whose cwd is unrelated
    # — not just user_prompt. asst_msg work-turns also carry cwd, and skipping
    # them here is exactly how another project's work (Edu) leaked onto this
    # board (#508). Events with no cwd (memory/plans/convo) are kept by
    # _cwd_in_project's empty-cwd rule.
    scoped = [e for e in events if _event_in_project(e, project)]
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


def _extract_haiku(project: Path, board: Path, card_py: Path,
                   buckets: dict[int, list[dict]], chunks: list[list[int]],
                   sorted_buckets: list[int], events: list[dict],
                   bucket_min: int, workers: int, chunk_size: int,
                   days: int, date_filter: str | None,
                   show_lifecycle: bool, pace_s: float,
                   reconcile: bool, phase: str = "",
                   will_reconcile: bool = False,
                   cards_offset: int = 0) -> tuple[int, list[int]]:
    """HAIKU mode: parallel per-chunk extraction → emit cards as chunks finish,
    snapshot the result, then reconcile. The autonomous (costs-Haiku) path.
    phase (#327) tags the HUD: 'replay' (tier-1) / 'speedup' (tier-2) / 'solo'.

    `will_reconcile` (#recon-handoff): True when a reconcile sweep runs AFTER
    this window (the tier-fly path runs recon outside the window, so this window
    is told `reconcile=False` yet must NOT flash '✓ COMPLETE' — it has to hand
    off to the reconcile stage on the same HUD).

    `cards_offset` (#hud-cumulative): cards already emitted by EARLIER tiers of
    the same fly-in. The HUD's "N cards emitted so far" line adds it so the
    count CARRIES OVER across the tier-1→tier-2 boundary instead of resetting to
    0 ('speeding up' must continue from the replay tier's total, not restart).

    Returns (n_cards, failed_buckets): this window's emitted count (so the caller
    can accumulate the offset) and the bucket keys that hard-failed even after the
    recovery pass (#627 — so the tier-fly records/surfaces them, never silent)."""
    t0 = time.monotonic()
    # Progress banner: a single 'notes' card the user can watch update live.
    banner_num = _banner_create(card_py, board, len(chunks), phase)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    n_cards = 0
    # #627: buckets whose extraction HARD-FAILED (ChunkExtractionError or a
    # crashed worker), as opposed to buckets that legitimately had no work.
    # Tracked so a failure is recorded + retried instead of silently dropped.
    failed_buckets: list[int] = []

    def _emit_chunk_cards(cards: list[dict], chunk_keys: list[int], *,
                          board, card_py, show_lifecycle, pace_s) -> int:
        """Emit a chunk's cards to the board; return how many flew. Shared by the
        main pass and the #627 recovery pass. The board-write context is passed
        explicitly (not closed over) so the static name-audit stays strict."""
        label_summary = " + ".join(_bucket_label(k, bucket_min)
                                   for k in chunk_keys)
        first_bucket_ts = datetime.fromtimestamp(
            chunk_keys[0] * bucket_min * 60, tz=timezone.utc).isoformat()
        emitted = 0
        for card in cards:
            card["_bucket_label"] = label_summary
            card["_bucket_ts_iso"] = first_bucket_ts
            num = emit_card(card_py, board, card, show_lifecycle, pace_s)
            if num:
                emitted += 1
            time.sleep(pace_s)
        return emitted

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
            label_summary = " + ".join(_bucket_label(k, bucket_min)
                                       for k in chunk_keys)
            try:
                cards = fut.result()
            except ChunkExtractionError:
                # Genuine extraction failure (not an empty bucket) — record the
                # buckets for the recovery pass instead of swallowing as 0 cards.
                cards = []
                failed_buckets.extend(chunk_keys)
                print(f"  ! chunk FAILED (recorded for retry): [{label_summary}]",
                      file=sys.stderr)
            except Exception as e:
                cards = []
                failed_buckets.extend(chunk_keys)
                print(f"  ! chunk error (recorded for retry): {e}",
                      file=sys.stderr)
            completed += 1
            print(f"  [{completed}/{len(chunks)}] [{label_summary}]  "
                  f"→ {len(cards)} card(s) extracted",
                  file=sys.stderr)
            n_cards += _emit_chunk_cards(
                cards, chunk_keys, board=board, card_py=card_py,
                show_lifecycle=show_lifecycle, pace_s=pace_s)
            # Drive the HUD after each chunk completes (the notes-column banner
            # card is gone; banner_num is None). #327 — on the LAST chunk of a
            # 'replay' tier (tier-2 still to come), swap the generic progress
            # line for a "day-1 replayed in Xs · speeding up ▸▸" handoff so the
            # HUD signals acceleration instead of flashing "✓ COMPLETE".
            handoff = None
            if phase == "replay" and completed == len(chunks):
                handoff = (f"day-1 replayed in {time.monotonic() - t0:.0f}s "
                           f"· speeding up ▸▸ backfilling older history")
            # #327 single-HUD: the LAST chunk completes the HUD ONLY when nothing
            # follows — i.e. no reconcile sweep (neither in-window `reconcile` nor
            # an after-window `will_reconcile`) AND this isn't the 'replay' tier
            # (replay always hands off to 'speedup'). Otherwise it hands off and
            # the HUD stays visible for the next stage (no flash/disappear). Without
            # `will_reconcile` the tier-fly speedup/solo tier wrongly flashed '✓
            # COMPLETE' before the end-of-replay reconcile re-showed the HUD.
            is_final = (completed == len(chunks) and not reconcile
                        and not will_reconcile and phase != "replay")
            _banner_update(card_py, board, banner_num,
                           completed, len(chunks), cards_offset + n_cards,
                           phase=phase, label_override=handoff, final=is_final)

    # #627 recovery pass: one more BOUNDED attempt at each hard-failed bucket so a
    # transient LLM error (timeout, cold-start, a flaky exit) doesn't silently
    # drop a whole range of cards. Per-bucket granularity (the failed chunk's
    # buckets were spread into failed_buckets). Anything still failing after this
    # is returned up so run() can record + surface it (never silently complete).
    if failed_buckets:
        print(f"  ↻ recovery: retrying {len(failed_buckets)} hard-failed "
              f"bucket(s) once more", file=sys.stderr)
        still_failed: list[int] = []
        for k in failed_buckets:
            try:
                cards = _extract_chunk_with_retries([k], buckets, project,
                                                    bucket_min)
            except ChunkExtractionError:
                still_failed.append(k)
                continue
            n_cards += _emit_chunk_cards(
                cards, [k], board=board, card_py=card_py,
                show_lifecycle=show_lifecycle, pace_s=pace_s)
        failed_buckets = still_failed
        if failed_buckets:
            print(f"  ⚠ {len(failed_buckets)} bucket(s) UNRECOVERED: "
                  f"[{', '.join(_bucket_label(k, bucket_min) for k in failed_buckets)}]"
                  f" — will be recorded, NOT silently dropped (#627)",
                  file=sys.stderr)

    # Save snapshot of post-extraction state BEFORE reconciliation, so
    # offline recon testing can iterate against a stable baseline.
    _save_snapshot(board, events, {
        "project": str(project), "days": days, "bucket_min": bucket_min,
        "chunk_size": chunk_size, "date_filter": date_filter,
        "n_buckets": len(sorted_buckets), "n_chunks": len(chunks),
        "n_cards": n_cards,
        "failed_buckets": list(failed_buckets),
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
          f"in {len(chunks)} chunk(s); recon moved {n_moved} card(s); "
          f"{len(failed_buckets)} bucket(s) unrecovered",
          file=sys.stderr)
    return n_cards, failed_buckets


def _run_window(project: Path, board: Path, card_py: Path, *,
                days: int, end_days_ago: int, show_lifecycle: bool,
                pace_s: float, phase: str, seed_if_empty: bool,
                sources, date_filter, bucket_min: int, recent_first: bool,
                max_buckets: int, chunk_size: int, workers: int,
                reconcile: bool, mode: str,
                will_reconcile: bool = False,
                cards_offset: int = 0) -> tuple[int, list[int]]:
    """Extract ONE history window: events → filter → bucketize → emit. The
    reusable unit behind BOTH the single-pass fill and the #327 two-tier fly,
    so tiering has one source of truth (no duplicate orchestration in bash).

    `will_reconcile` is forwarded to `_extract_haiku` so the LAST tier of a
    tier-fly hands off to the reconcile stage instead of flashing '✓ COMPLETE'.

    `cards_offset` is forwarded so the HUD card-count carries across tiers.
    Returns (n_cards, failed_buckets) — the emitted count (0 if nothing
    extracted) plus any hard-failed buckets (#627), both empty on early exit."""
    events = _flatten_events(project, days, sources=sources)
    if not events:
        print(f"no events to extract (phase={phase or '-'})", file=sys.stderr)
        return 0, []
    events = _filter_events(events, project, date_filter, end_days_ago,
                            seed_if_empty=seed_if_empty)
    if events is None:
        return 0, []
    sorted_buckets, buckets, chunks = _bucketize(
        events, bucket_min, recent_first, max_buckets, chunk_size)
    print(f"▶ hourly extraction [{phase or 'single'}]: {len(sorted_buckets)} "
          f"bucket(s) of {len(events)} events → {len(chunks)} chunk(s) of "
          f"≤{chunk_size} bucket(s) (mode={mode}, workers={workers})",
          file=sys.stderr)
    if mode == "inline":
        n = _emit_extraction_pending(board, card_py, chunks, buckets,
                                     bucket_min, project)
        print(f"✓ inline extraction staged: {n} chunk(s) await main Claude",
              file=sys.stderr)
        return 0, []
    return _extract_haiku(project, board, card_py, buckets, chunks,
                          sorted_buckets, events, bucket_min, workers,
                          chunk_size, days, date_filter, show_lifecycle,
                          pace_s, reconcile, phase=phase,
                          will_reconcile=will_reconcile,
                          cards_offset=cards_offset)


def run(project: Path, board: Path, port: int, days: int,
        show_lifecycle: bool, pace_s: float,
        max_buckets: int, workers: int = 8,
        bucket_min: int = 60, chunk_size: int = 1,
        date_filter: str | None = None,
        reconcile: bool = True,
        snapshot_load: Path | None = None,
        end_days_ago: int = 0,
        recent_first: bool = False,
        mode: str = "haiku",
        sources: set | None = None,
        seed_if_empty: bool = False,
        phase: str = "",
        tier_fly: bool = False) -> None:
    card_py = Path(__file__).resolve().parent / "card.py"
    if not card_py.exists():
        print(f"card.py not found at {card_py}", file=sys.stderr)
        return

    # --snapshot-load: skip extraction, just rehydrate board + run recon.
    if snapshot_load:
        _run_snapshot_load(board, card_py, snapshot_load, reconcile)
        return

    # NB: reconcile is NOT in `common` — it's passed per-window so the two-tier
    # fly reconciles exactly ONCE (after the LAST tier), not after every tier.
    # A per-tier reconcile is what made the HUD complete+hide then reappear.
    common = dict(sources=sources, date_filter=date_filter,
                  bucket_min=bucket_min, recent_first=recent_first,
                  max_buckets=max_buckets, chunk_size=chunk_size,
                  workers=workers, mode=mode)

    # #327 — TWO-TIER FLY (one source of truth; serve_bootstrap AND install.sh
    # --harvest both pass --tier-fly). Haiku + multi-day → run the WATCHED
    # tier-1 (last 1d, lifecycle flights, 'replay') then the faster 'speeding
    # up' tier-2 backfill (older history, lifecycle, 5× faster pace). days==1 →
    # one 'solo' pass. Both tiers FORCE --show-lifecycle so they actually FLY
    # (the harvest path used to add cards flat = a pop, not a fly).
    if tier_fly and mode == "haiku":
        # Anchor the whole fly-in on the project's LAST session, not on `now`.
        # off = days since last work (0 = worked today). Shifting both the
        # window length AND the tier boundary by `off` slides the window back so
        # it covers the `days` days of ACTUAL work ending at the last session.
        # Example: last session 5d ago, days=2 → tier-1 [now-6,now-5] (most
        # recent work day) + tier-2 [now-7,now-6] (older day) = work over the
        # 7d-ago→5d-ago span. off=0 reduces to the legacy now-anchored windows.
        off = _anchor_offset_days(project)
        # Gate (#recon-after-replay): completed_card_replay = 0 for the whole
        # fly-in. NO tier reconciles mid-replay (reconcile=False on every
        # window) — a recon racing the live emits is what made cards shuffle.
        # The single sweep runs below, ONCE, only after the gate flips to 1.
        _mark_replay_started(board, days)
        if off:
            print(f"  anchor: last session ~{off}d ago → fly window slides to "
                  f"cover {days}d of work ending then (not an empty recent gap)",
                  file=sys.stderr)
        # #627: collect buckets that hard-failed across BOTH tiers so the gate is
        # flipped HONESTLY (partial) and the drop is surfaced, never silent.
        failed_buckets: list[int] = []
        if days > 1:
            # Carry tier-1's emitted count into tier-2 so the HUD's "N cards
            # emitted so far" continues from the replay total instead of
            # resetting to 0 when it 'speeds up' (#hud-cumulative).
            n_replay, fail_replay = _run_window(
                project, board, card_py, days=off + 1, end_days_ago=off,
                show_lifecycle=True, pace_s=pace_s, phase="replay",
                seed_if_empty=seed_if_empty, reconcile=False, **common)
            _n_spd, fail_speedup = _run_window(
                project, board, card_py, days=off + days,
                end_days_ago=off + 1,
                show_lifecycle=True, pace_s=max(pace_s / 5, 0.0),
                phase="speedup", seed_if_empty=False,
                reconcile=False, will_reconcile=reconcile,
                cards_offset=n_replay, **common)
            failed_buckets = fail_replay + fail_speedup
        else:
            _n_solo, failed_buckets = _run_window(
                project, board, card_py, days=off + 1, end_days_ago=off,
                show_lifecycle=True, pace_s=pace_s, phase="solo",
                seed_if_empty=seed_if_empty, reconcile=False,
                will_reconcile=reconcile, **common)

        # Replay of the past N days is COMPLETE → reconcile EXACTLY ONCE against
        # the whole replay span (not just the last tier's older slice — so an
        # In-Progress→Done that shipped on the most recent day is still caught).
        # Order matters: reconcile FIRST, flip the gate AFTER. The replay gate
        # (_replay_complete) must stay CLOSED for the duration of this sweep so a
        # SessionStart `--reconcile-only` pass firing in this ~10s window stands
        # down instead of racing it (the "reconcile twice / cards shuffle then
        # 'N up to date' again" bug). recon_lock is the belt; this is the braces.
        # try/finally (#384 RECON-GATE-STUCK): the gate MUST reopen even if the
        # sweep raises. Without it, an exception in reconcile_sweep leaves
        # completed_card_replay=0 forever → every future SessionStart recon for
        # this board stands down permanently (the gate never flips back to 1).
        try:
            if reconcile:
                events = _flatten_events(project, off + days, sources=sources)
                events = _filter_events(events, project, date_filter, off) or []
                if events:
                    n_moved = reconcile_sweep(card_py, board, events)
                    print(f"✓ end-of-replay reconcile: moved {n_moved} card(s)",
                          file=sys.stderr)
        finally:
            # #627: stamp the gate with the partial-failure record. Gate STILL
            # reopens (completed_card_replay=1) so recon isn't stuck (#384), but
            # the dropped buckets are recorded rather than declared a clean fill.
            _mark_replay_complete(board, failed_buckets=failed_buckets)
        # #627: a partial fill must be VISIBLE — loud stderr + a degraded final
        # HUD instead of a clean "✓ COMPLETE", so the user knows to re-run.
        if failed_buckets:
            msg = (f"⚠ {len(failed_buckets)} time-bucket(s) couldn't be read — "
                   f"re-run bootstrap to backfill them")
            print(f"⚠ bootstrap finished PARTIAL: {msg}", file=sys.stderr)
            try:
                _banner_update(card_py, board, None, 1, 1, 0,
                               phase="reconcile", label_override=msg, final=True)
            except Exception:
                pass
        return

    # Single pass — inline staging, or an explicit non-tier haiku run.
    _run_window(project, board, card_py, days=days, end_days_ago=end_days_ago,
                show_lifecycle=show_lifecycle, pace_s=pace_s, phase=phase,
                seed_if_empty=seed_if_empty, reconcile=reconcile, **common)


# Calibrated wall-time of one haiku extraction call (chunk), thinking OFF
# (MAX_THINKING_TOKENS=0). Measured ~5–7s/call; 6 is the planning midpoint. Used
# only for the upfront "≈N min to fill" estimate shown to the user at bootstrap.
_SEC_PER_CHUNK = 6.0


def estimate_fill(project: Path, days: int, bucket_min: int, chunk_size: int,
                  workers: int, sources: set | None) -> dict:
    """Cheap pre-flight estimate of the fly-in: harvest + bucketize ONLY (no
    haiku), so we can tell the user "≈N min to fill, grab a coffee" upfront.

    Mirrors the tier-fly window: anchors on the project's last session (offset),
    covering `days` days of work ending there — the combined span of tier-1 +
    tier-2 is one window [now-(off+days), now-off], so a single bucketize gives
    the total chunk count. eta = ceil(chunks/workers) * _SEC_PER_CHUNK."""
    import math
    off = _anchor_offset_days(project)
    full_days = off + max(days, 1)
    events = _flatten_events(project, full_days, sources=sources)
    chunks = 0
    buckets = 0
    if events:
        scoped = _filter_events(events, project, None, off, seed_if_empty=False)
        if scoped:
            sorted_buckets, _b, chunk_list = _bucketize(
                scoped, bucket_min, True, 0, chunk_size)
            buckets = len(sorted_buckets)
            chunks = len(chunk_list)
    eta_sec = int(math.ceil(chunks / max(workers, 1)) * _SEC_PER_CHUNK) if chunks else 0
    return {"offset_days": off, "buckets": buckets, "chunks": chunks,
            "eta_sec": eta_sec, "eta_min": round(eta_sec / 60.0, 1)}


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
    ap.add_argument("--workers", type=int, default=8,
                    help="parallel LLM workers (default 8 — measured 2x vs 4, "
                         "0 failures; raise to 12 for huge windows)")
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
    ap.add_argument("--mode", choices=["haiku"], default="haiku",
                    help="haiku = claude -p per chunk (costs usage). "
                         "('inline' staging is retired — no longer selectable; the "
                         "engine code in hourly_reconcile._emit_extraction_pending "
                         "remains but is unreachable from the CLI)")
    ap.add_argument("--phase", type=str, default="",
                    help="#327 HUD fill-stage tag: replay (tier-1 watched) / "
                         "speedup (tier-2 backfill) / solo (single tier) / "
                         "'' (inline default). Sets the live HUD header.")
    ap.add_argument("--tier-fly", action="store_true",
                    help="#327 two-tier fly: haiku + days>1 → watched tier-1 "
                         "(last 1d, lifecycle) then the faster 'speeding up' "
                         "tier-2 backfill. days==1 → single 'solo' pass. Owns "
                         "tiering so callers don't duplicate it.")
    ap.add_argument("--seed-cross-project-if-empty", action="store_true",
                    help="#285: if this project has NO local history (fresh "
                         "adopter), seed the first fill from recent cross-project "
                         "history so the board isn't blank on day one. Off by "
                         "default (strict project scope); bootstrap turns it on.")
    ap.add_argument("--estimate-only", action="store_true",
                    help="don't extract — harvest+bucketize only and print a "
                         "JSON fill estimate {chunks,eta_sec,eta_min} so the "
                         "caller can tell the user '≈N min to fill'. No haiku.")
    ap.add_argument("--reconcile-only", action="store_true",
                    help="SessionStart smart-recon: skip extraction entirely; "
                         "reconcile every non-done card against activity since "
                         "the last recon (gated to avoid a Haiku call when "
                         "there's nothing new). No card creation.")
    args = ap.parse_args()
    if args.reconcile_only:
        _run_reconcile_only(args.project.resolve(), args.board.resolve())
        return
    if args.estimate_only:
        est = estimate_fill(
            args.project.resolve(), args.days, args.bucket_min,
            args.chunk_size, args.workers,
            ({s.strip() for s in args.sources.split(",") if s.strip()}
             if args.sources else None))
        print(json.dumps(est))
        return
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
        seed_if_empty=args.seed_cross_project_if_empty,
        phase=args.phase,
        tier_fly=args.tier_fly)


if __name__ == "__main__":
    main()
