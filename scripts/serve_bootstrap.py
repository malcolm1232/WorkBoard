#!/usr/bin/env python3
"""board-steward bootstrap & discovery helpers — extracted from serve.py (#307 file-split).

First-run / install-time machinery: seed a new board from the template, detect
the project name, manage the .gitignore block, and stream cards discovered from
~/.claude history (the "History Replay" backfill) or the hourly extractor. All
self-contained — no SSE/handler/runtime state from serve.py — so serve.py imports
the three entry points (bootstrap_board, _stream_discovered_cards,
_stream_hourly_cards) back, with no circular dependency.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_HTML = SKILL_DIR / "templates" / "board.html"
TEMPLATE_JSON = SKILL_DIR / "templates" / "board.json"


TAG_PROFILES = SKILL_DIR / "templates" / "tag-profiles.json"

def _load_tag_profile(profile: str) -> dict:
    """Read tag-profiles.json and return one profile's main+sub. Falls back to
    software if the requested profile doesn't exist."""
    if not TAG_PROFILES.exists():
        return {"profile": profile, "main": [], "sub": []}
    profiles = json.loads(TAG_PROFILES.read_text())
    chosen = profiles.get(profile) or profiles.get("software") or {}
    return {
        "profile": profile if profile in profiles else "software",
        "main": chosen.get("main", []),
        "sub":  chosen.get("sub", []),
    }


def _detect_project_name(project_root: Path) -> str:
    """Pick the friendliest project name we can find. Order of preference:
    package.json `name`, pyproject.toml `project.name`, CONTEXT.md first H1,
    cwd basename. Falls back to 'WorkBoard' if everything is empty."""
    pkg = project_root / "package.json"
    if pkg.is_file():
        try:
            name = json.loads(pkg.read_text()).get("name", "").strip()
            if name:
                return name
        except Exception:
            pass
    pyproj = project_root / "pyproject.toml"
    if pyproj.is_file():
        try:
            m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']',
                          pyproj.read_text(), re.MULTILINE)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    ctx = project_root / "CONTEXT.md"
    if ctx.is_file():
        try:
            for line in ctx.read_text().splitlines():
                m = re.match(r'^#\s+(.+?)\s*$', line)
                if m:
                    return m.group(1).strip()
        except Exception:
            pass
    return project_root.name or "WorkBoard"


_GITIGNORE_BLOCK = "# Added by board-steward — local board data may contain secrets"
_GITIGNORE_ENTRIES = (
    "board/",
    "board/board.json",
    "board/index.json",
    "board/extraction_snapshot.json",
    "board/recon_pending.json",
)


def _ensure_gitignore(project_root: Path) -> str | None:
    """Idempotently append board artifacts to project's .gitignore if the
    project is a git repo. Returns a one-line status for the caller to print,
    or None if nothing to do (not a repo). Safe to call repeatedly."""
    if not (project_root / ".git").exists():
        return None
    gi = project_root / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    missing = [e for e in _GITIGNORE_ENTRIES if e not in existing_lines]
    if not missing:
        return f"{gi} already covers board artifacts"
    block = "\n" + _GITIGNORE_BLOCK + "\n" + "\n".join(missing) + "\n"
    if existing and not existing.endswith("\n"):
        block = "\n" + block
    with gi.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return f"{gi} updated (+{len(missing)} entries)"


def bootstrap_board(board_dir: Path, profile: str = "software",
                    title_override: str | None = None,
                    share: bool = False) -> None:
    """First-run init: create board/ with starter board.json + board.html.
    Title resolves via `_detect_project_name` (package.json / pyproject.toml /
    CONTEXT.md / basename) unless `title_override` is given. Tag taxonomy
    seeded from the chosen industry profile. When `share` is False (default)
    and the project is a git repo, board artifacts are added to .gitignore so
    user-private content (titles, origins, secrets) doesn't get committed."""
    board_dir.mkdir(parents=True, exist_ok=True)
    target_json = board_dir / "board.json"
    if not target_json.exists() and TEMPLATE_JSON.exists():
        data = json.loads(TEMPLATE_JSON.read_text())
        project_name = title_override or _detect_project_name(board_dir.parent)
        data["title"] = f"WorkBoard — {project_name}"
        data["tagTaxonomy"] = _load_tag_profile(profile)
        target_json.write_text(json.dumps(data, indent=2))
    target_html = board_dir / "board.html"
    if not target_html.exists() and TEMPLATE_HTML.exists():
        target_html.write_text(TEMPLATE_HTML.read_text())
    if not share:
        status = _ensure_gitignore(board_dir.parent)
        if status:
            print(f"gitignore: {status}", file=sys.stderr)


def _classify_column(real_ship: bool, urgency: list, defer: list, bugs: list,
                     age_days: int, has_files: bool) -> str:
    """Heuristic: map a discovered task's signals to a board column. Branch
    ORDER is significant — a bugs-only task lands in backlog even if it's old
    with file activity (the bugs check precedes the stale-with-files → done
    rule), matching the original inline chain exactly."""
    if real_ship:
        return "done"
    if urgency:                            # urgency-language → mandatory
        return "mandatory"
    if defer:
        return "backlog"
    if age_days <= 2 and has_files:
        return "inprogress"
    if age_days <= 3 and not has_files:
        return "task"
    if bugs:
        return "backlog"
    if age_days > 7 and has_files:
        return "done"
    return "backlog"                       # stale-no-files / default


def _build_card_notes(task: dict, files_proj: list, files_all: list,
                      commits: list, ship: list, defer: list,
                      bugs: list) -> str:
    """Assemble the multi-line notes body (task summary + files + commits +
    ship/defer/bug signals) for a discovered card."""
    parts: list[str] = []
    dur = task.get("duration_min", 0)
    n_user = task.get("n_user_total", 0)
    src = ", ".join(task.get("source_set") or [])
    parts.append(f"Task: {n_user} user turn(s) over {dur}min. Sources: {src}.")
    if files_proj:
        parts.append("In-proj files: " + ", ".join(files_proj[:8])
                     + ("..." if len(files_proj) > 8 else ""))
    elif files_all:
        parts.append("Files touched: " + ", ".join(
            Path(f).name for f in files_all[:8]))
    if commits:
        parts.append("Commits: " + " / ".join(
            f"{c.get('shaShort') or c.get('sha','')[:7]} {c.get('subj','')[:60]}"
            for c in commits[:3]))
    if ship:
        parts.append("Ship signals: " + " / ".join(s[:120] for s in ship[:3]))
    if defer:
        parts.append("Defer signals: " + " / ".join(s[:120] for s in defer[:3]))
    if bugs:
        parts.append("Bug signals: " + " / ".join(s[:120] for s in bugs[:3]))
    return "\n".join(parts)


def _task_to_card_args(task: dict) -> list[str] | None:
    """Map a discover2.py task record to `card.py add` CLI args. Uses both
    files_touched_all (work intensity) and files_touched_in_proj (relevance),
    so sessions that edit sibling-repo notes still get credit (Bug 1 fix)."""
    title_seed = (task.get("user_prompt") or "").strip()
    files_all = task.get("files_touched_all") or []
    files_proj = task.get("files_touched_in_proj") or []
    ship = task.get("ship_hits_clean") or []
    defer = task.get("defer_hits") or []
    bugs = task.get("bug_hits") or []
    commits = task.get("git_commits") or []

    if not title_seed and not files_all and not commits:
        return None
    if len(title_seed) < 15 and not files_all and not ship and not commits:
        return None

    title = (title_seed[:80]
             or (f"Worked on {Path(files_all[0]).name}" if files_all else "Task"))
    if "\n" in title:
        title = title.split("\n", 1)[0]
    title = title.rstrip()

    # Column heuristic — ship needs BOTH a clean ship hit AND file activity OR
    # a git commit landed inside the task window.
    try:
        ended_dt = datetime.fromisoformat(
            (task.get("ts_end") or "").replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - ended_dt).days
    except Exception:
        age_days = 999

    real_ship = (bool(ship) and bool(files_all)) or bool(commits)
    urgency = task.get("urgency_hits") or []
    column = _classify_column(real_ship, urgency, defer, bugs,
                              age_days, bool(files_all))

    tags = []
    if bugs: tags.append("bug")
    if defer: tags.append("deferred")
    if real_ship: tags.append("shipped")
    if urgency: tags.append("mandatory")

    ended = (task.get("ts_end") or "")[:10]
    sid = (task.get("sessionId") or "")[:8]
    origin = (f"Discovered by discover2 (bucket {task.get('bucket_id')}, "
              f"session {sid}, {ended}). User said: \"{title_seed[:300]}\"")

    notes = _build_card_notes(task, files_proj, files_all,
                              commits, ship, defer, bugs)

    args = ["--column", column, "--priority", "mid", "--title", title,
            "--origin", origin, "--notes", notes, "--tag", "discovered"]
    for t in tags:
        args += ["--tag", t]
    return args


def _session_to_card_args(session: dict) -> list[str] | None:
    """Map a discover.py session summary to `card.py add` CLI args. Returns
    None if the session is too thin to be worth carding."""
    first = (session.get("firstUserPrompt") or "").strip()
    files = session.get("filesEdited") or []
    ship = session.get("shipHints") or []
    defer = session.get("deferHints") or []
    bugs = session.get("bugHints") or []

    # Confidence filter — skip sessions with no real signal.
    if not first and not files:
        return None
    if len(first) < 15 and not files and not ship:
        return None

    title = (first[:80] or (f"Worked on {Path(files[0]).name}" if files else "Session")).rstrip()
    if "\n" in title:
        title = title.split("\n", 1)[0]

    # Column heuristic — spread cards across Task / In Progress / Backlog / Done
    # by recency + signal density. The board should reflect last-week-of-work
    # mix, not be a graveyard of "Done".
    try:
        ended_dt = datetime.fromisoformat(
            (session.get("endedAt") or "").replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - ended_dt).days
    except Exception:
        age_days = 999

    # SHIP_RE matches "done|fixed|works|live" — Claude says those casually in
    # nearly every reply, so a ship-hint alone is noisy. Trust it only when
    # there's actual file activity to back it up.
    real_ship = bool(ship) and bool(files)

    if real_ship:
        column = "done"                          # actually shipped work
    elif defer:
        column = "backlog"                       # explicit defer wins
    elif age_days <= 2 and files:
        column = "inprogress"                    # recent + editing → in flight
    elif age_days <= 3 and not files:
        column = "task"                          # recent talk, no work yet
    elif bugs and not real_ship:
        column = "backlog"                       # open bug
    elif age_days > 7 and files:
        column = "done"                          # old work with edits → done
    elif age_days > 7:
        column = "backlog"                       # old chatter, no work
    else:
        column = "backlog"                       # default: mentioned, unactioned

    tags = []
    if bugs: tags.append("bug")
    if defer: tags.append("deferred")
    if real_ship: tags.append("shipped")

    ended = (session.get("endedAt") or "")[:10]
    sid = (session.get("sessionId") or "")[:8]
    origin = f"Discovered from session {sid} ({ended}). User said: \"{first[:300]}\""

    notes_parts = []
    dur = session.get("durationMin", 0)
    turns = session.get("turns", {})
    notes_parts.append(f"Session: {turns.get('user',0)} user / {turns.get('assistant',0)} asst turns over {dur}min.")
    if files:
        notes_parts.append("Files: " + ", ".join(files[:10]) + ("..." if len(files) > 10 else ""))
    if ship:
        notes_parts.append("Ship signals: " + " / ".join(s[:120] for s in ship[:3]))
    if defer:
        notes_parts.append("Defer signals: " + " / ".join(s[:120] for s in defer[:3]))
    if bugs:
        notes_parts.append("Bug signals: " + " / ".join(s[:120] for s in bugs[:3]))
    notes = "\n".join(notes_parts)

    args = ["--column", column, "--priority", "mid", "--title", title,
            "--origin", origin, "--notes", notes, "--tag", "discovered"]
    for t in tags:
        args += ["--tag", t]
    return args


def _stream_discovered_cards(project_root: Path, board_dir: Path,
                              port: int, days: int, max_items: int,
                              delay_s: float = 0.25,
                              legacy: bool = False,
                              harvest_root: Path | None = None,
                              seed_if_empty: bool = False) -> None:
    """Background-thread worker: run discover2.py (or discover.py if --legacy),
    then issue `card.py add` for each task at `delay_s` pacing. Cards land via
    the live HTTP server, which fires SSE events — the browser animates them in.

    harvest_root mines history from a different dir than the board lives in
    (isolated sim/--demo); defaults to the board's own project."""
    script_dir = Path(__file__).resolve().parent
    discover_py = script_dir / ("discover.py" if legacy else "discover2.py")
    card_py = script_dir / "card.py"
    if not discover_py.exists() or not card_py.exists():
        return
    project_root = harvest_root or project_root

    for _ in range(20):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.3).read()
            break
        except Exception:
            time.sleep(0.2)

    cmd = [sys.executable, str(discover_py),
           "--project", str(project_root),
           "--days", str(days)]
    cmd += ["--max-sessions" if legacy else "--max-tasks", str(max_items)]
    if seed_if_empty and not legacy:  # #285 (discover2 only; legacy has no flag)
        cmd += ["--seed-cross-project-if-empty"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return
        data = json.loads(out.stdout)
    except Exception:
        return

    if legacy:
        items = data.get("sessions", [])
        mapper = _session_to_card_args
    else:
        items = data.get("tasks", [])
        mapper = _task_to_card_args
    if not items:
        return

    n_added = 0
    for item in items[:max_items]:
        args = mapper(item)
        if not args:
            continue
        try:
            subprocess.run(
                [sys.executable, str(card_py), "--board",
                 str(board_dir / "board.json"), "add"] + args,
                capture_output=True, text=True, timeout=10,
            )
            n_added += 1
        except Exception:
            pass
        time.sleep(delay_s)

    print(f"discover{'(legacy)' if legacy else '2'}: streamed {n_added} card(s) "
          f"from {len(items)} {'session' if legacy else 'task'}(s)",
          file=sys.stderr)


def _stream_hourly_cards(project_root: Path, board_dir: Path, port: int,
                          days: int, bucket_min: int = 30,
                          chunk_size: int = 2,
                          harvest_root: Path | None = None,
                          mode: str = "inline",
                          seed_if_empty: bool = False) -> None:
    """Background-thread worker: the HIGH-COMPUTE startup fill (card #265/#268).

    Runs hourly_extractor.py over the project's full history — multi-source
    harvest (jsonl + auto-memory + convo dumps + plans + git) bucketed by
    `bucket_min`, one `claude -p haiku` call per `chunk_size` buckets — and
    flies each resulting WORK-UNIT card task→inprogress→done. This is the
    quality path the user chose as the install/startup behaviour, replacing the
    cheap discover2 'plop'. Compute-heavy by design (#264 tracks a light rework).

    seed_if_empty (#285): on a fresh repo with no local history, seed the first
    fill from recent cross-project history so day one isn't blank.

    Needs the `claude` CLI on PATH for haiku mode; if that's absent the board
    falls back to the cheap discover path rather than staying empty."""
    script_dir = Path(__file__).resolve().parent
    extractor = script_dir / "hourly_extractor.py"
    if not extractor.exists():
        # Fall back to the cheap discover path rather than leaving it blank.
        _stream_discovered_cards(project_root, board_dir, port, days, 20)
        return

    for _ in range(20):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.3).read()
            break
        except Exception:
            time.sleep(0.2)

    # harvest_root lets the board live in one dir while history is mined from
    # another (the isolated-sim / --demo case). Defaults to the board's own
    # project — the normal same-project install.
    base = [sys.executable, str(extractor),
            "--project", str(harvest_root or project_root),
            "--board", str(board_dir / "board.json"),
            "--port", str(port),
            "--bucket-min", str(bucket_min),
            "--chunk-size", str(chunk_size),
            "--recent-first", "--mode", mode]
    if seed_if_empty:
        base += ["--seed-cross-project-if-empty"]  # #285 never-empty day one

    # INLINE (default, free): one fast pass over the whole window that stages
    # extraction_pending.json — main Claude (the session the user is already in)
    # emits the cards at no Haiku cost. No fly/tiering here: staging is instant.
    if mode == "inline":
        print("inline bootstrap: staging extraction_pending.json (no Haiku)",
              file=sys.stderr)
        try:
            subprocess.run(base + ["--days", str(days)], timeout=600)
        except Exception as e:
            print(f"inline bootstrap stage failed: {e}", file=sys.stderr)
        return

    # HAIKU (opt-in): two-tier fly fill so the user can start working while it
    # backfills. TIER 1 = last 1d (fast); TIER 2 = older history, in background.
    # Daemon thread; writes serialize through the server lock — no corruption.
    base += ["--show-lifecycle"]
    tiers = [("tier-1 (last 1d)", ["--days", "1"])]
    if days > 1:
        tiers.append((f"tier-2 (older, ≤{days}d)",
                      ["--days", str(days), "--end-days-ago", "1"]))
    for label, extra in tiers:
        print(f"hourly bootstrap fill: {label}", file=sys.stderr)
        try:
            subprocess.run(base + extra, timeout=3600)
        except Exception as e:
            print(f"hourly bootstrap fill {label} failed: {e}", file=sys.stderr)

