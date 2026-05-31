#!/usr/bin/env python3
"""discover2.py — time-bucketed cross-source work extractor.

Replaces discover.py. Three bug fixes + one structural change:
  bug 1: filesEdited filter — keep both raw and in-project so ship gate
         doesn't lose signal when a session edits notes/plans/sibling repos.
  bug 2: SKIP_RE undercount — count user turns BEFORE classifying them, so
         short replies don't vanish from the turn total.
  bug 3: SHIP_RE false positives — require either a strong word
         (shipped|deployed|merged|landed|verified) OR a weak word with a
         file edit in the same 60s window. Sentence-final 'Done.' subsection
         closers no longer count.

Structural: output is task-shaped, not session-shaped. A bucket is 10 minutes
(configurable). Within a bucket, substantive prompts seed tasks; short / marker /
fast-follow prompts merge. Pass 2 walks the task list once more and stitches
adjacent fragments sharing files into one card.

Sources harvested (all silent, no user prompts on default install):
  ~/.claude/projects/*/*.jsonl                       — session turns + tool_use
  ~/.claude/projects/*/memory/*.md                   — auto-memory mtime
  conversation_{raw,verbatim}_*.md (dir auto-derived) — manual dumps
  ~/.claude/plans/*.md                               — plan mtime
  git log on the project repo                        — commits with timestamps

Stdlib only.

Usage:
    discover2.py                          # cwd, last 7 days
    discover2.py --project /path/to/repo
    discover2.py --days 30 --bucket-min 10
    discover2.py --legacy                 # fall back to discover.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


# Extracted modules (#307 file-split). Leaves; re-exported so external
# importers (hourly_extractor: harvest_*/parse_ts) keep working unchanged.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover2_sources import *   # noqa: E402,F401,F403
from discover2_extract import *    # noqa: E402,F401,F403


# ---------- install-time ask + config ----------

def _config_path(project: Path) -> Path:
    return project / "board" / "discover.config.json"


def load_config(project: Path) -> dict:
    p = _config_path(project)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_config(project: Path, cfg: dict) -> None:
    p = _config_path(project)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


# A convo-dump file is conversation_raw_YYMMDD.md or conversation_verbatim_YYMMDD.md.
# A path token mentioning "conversation" and ending in .md (even a YYMMDD template)
# points at the dump dir via its parent — we validate the dir by globbing it.
_CONVO_PATH_RE = re.compile(r"[~./][^\s'\"`)]*conversation[^\s'\"`)]*\.md")


def _dir_has_convo_dumps(d: Path) -> bool:
    try:
        if not d.is_dir():
            return False
        return any(d.glob("conversation_raw_*.md")) or \
               any(d.glob("conversation_verbatim_*.md"))
    except OSError:
        return False


def _claude_md_files(project: Path) -> list[Path]:
    """CLAUDE.md locations, most-authoritative first (global, home, project)."""
    return [
        Path.home() / ".claude" / "CLAUDE.md",
        Path.home() / "CLAUDE.md",
        project / "CLAUDE.md",
    ]


def _convo_dir_from_text(text: str) -> Path | None:
    """Pull the first convo-dump dir referenced in arbitrary text (CLAUDE.md,
    a transcript line, a shell command). Validates the parent dir actually holds
    dumps so a stale/templated mention can't return a bogus path."""
    for m in _CONVO_PATH_RE.finditer(text):
        try:
            d = Path(m.group(0)).expanduser().parent
        except (ValueError, RuntimeError):
            continue
        if _dir_has_convo_dumps(d):
            return d
    return None


def _mine_convo_dir(project: Path) -> Path | None:
    """Derive the convo-dump dir from Claude-specific, always-present data —
    no hardcoded path, no interactive ask. Ladder:
      1. CLAUDE.md (global + home + project): users who keep dumps DOCUMENT the
         path/render-command there. Cheap (≤3 small files) and authoritative.
      2. ~/.claude/history.jsonl: the all-projects command chronicle — render
         invocations + paths land here. Single file, bounded scan.
      3. Recent session transcripts: tally the dir of any convo-dump path the
         user touched (Read/Write/Bash); most-frequent wins. Bounded to the
         newest N sessions so the scan stays cheap.
    Returns None if nothing is found — callers must treat convo as optional
    enrichment (we already harvest the raw *.jsonl directly)."""
    # 1. CLAUDE.md — most reliable signal.
    for cm in _claude_md_files(project):
        try:
            if cm.is_file():
                hit = _convo_dir_from_text(cm.read_text(errors="replace"))
                if hit:
                    return hit
        except OSError:
            continue

    # 2. global history.jsonl — one file, scan as text.
    hist = Path.home() / ".claude" / "history.jsonl"
    try:
        if hist.is_file():
            hit = _convo_dir_from_text(hist.read_text(errors="replace"))
            if hit:
                return hit
    except OSError:
        pass

    # 3. recent transcripts — tally dirs of touched convo-dump paths.
    root = Path.home() / ".claude" / "projects"
    if root.is_dir():
        try:
            jpaths = sorted(root.glob("*/*.jsonl"),
                            key=lambda p: p.stat().st_mtime, reverse=True)[:30]
        except OSError:
            jpaths = []
        tally: dict[str, int] = {}
        for jp in jpaths:
            try:
                text = jp.read_text(errors="replace")
            except OSError:
                continue
            for m in _CONVO_PATH_RE.finditer(text):
                try:
                    d = Path(m.group(0)).expanduser().parent
                except (ValueError, RuntimeError):
                    continue
                if _dir_has_convo_dumps(d):
                    tally[str(d)] = tally.get(str(d), 0) + 1
        if tally:
            best = max(tally, key=tally.get)
            return Path(best)
    return None


def find_convo_dir(project: Path, asked: bool = False) -> Path | None:
    """Resolve the convo-dump dir. Config override → auto-derive from Claude's
    own data (no hardcode, no ask) → legacy candidate dirs → None."""
    cfg = load_config(project)
    if cfg.get("convo_dir"):
        p = Path(cfg["convo_dir"]).expanduser()
        if p.is_dir():
            return p

    derived = _mine_convo_dir(project)
    if derived:
        # Cache so subsequent runs skip the scan.
        try:
            cfg["convo_dir"] = str(derived)
            save_config(project, cfg)
        except Exception:
            pass
        return derived

    # Legacy fallback: the old fixed candidate list.
    candidates = [
        Path.home() / "Desktop" / "conversation_history",
        Path.home() / "conversation_history",
        project / "conversation_history",
    ]
    for c in candidates:
        if _dir_has_convo_dumps(c):
            return c
    return None


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path, default=Path.cwd())
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--bucket-min", type=int, default=10)
    ap.add_argument("--max-tasks", type=int, default=40)
    ap.add_argument("--all-projects", action="store_true",
                    help="don't filter tasks by project")
    ap.add_argument("--seed-cross-project-if-empty", action="store_true",
                    help="#285: if the project filter yields NO tasks (fresh "
                         "adopter with no local history), fall back to the "
                         "unfiltered cross-project tasks so the board isn't "
                         "blank on day one. Off by default (strict scope).")
    ap.add_argument("--legacy", action="store_true",
                    help="fall back to discover.py")
    ap.add_argument("--ask-convo", action="store_true",
                    help="ask for conversation dir if not auto-found")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.legacy:
        legacy = Path(__file__).resolve().parent / "discover.py"
        os.execvp(sys.executable, [sys.executable, str(legacy),
                                   "--project", str(args.project),
                                   "--days", str(args.days)])
        return  # not reached

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)
             if args.days > 0 else None)
    project = args.project.resolve()

    convo_dir = find_convo_dir(project)
    if convo_dir is None and args.ask_convo and sys.stdin.isatty():
        # Polite one-time install-time ask — only fires interactively.
        print("Where do you keep your conversation history? (empty = skip)",
              file=sys.stderr)
        try:
            ans = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans:
            convo_dir = Path(ans).expanduser()
            if convo_dir.is_dir():
                cfg = load_config(project)
                cfg["convo_dir"] = str(convo_dir)
                save_config(project, cfg)

    # Harvest
    events: list[dict] = []
    jsonl_events = harvest_jsonl(since)
    events.extend(jsonl_events)
    seen_sessions = {(e.get("meta") or {}).get("sessionId") for e in jsonl_events}
    events.extend(harvest_history(since, exclude_sessions=seen_sessions))  # #282 gap-fill
    events.extend(harvest_memory(since))
    events.extend(harvest_plans(since))
    if convo_dir:
        events.extend(harvest_convo(since, convo_dir))
    events.extend(harvest_git(project, since))

    if args.debug:
        kinds: dict[str, int] = {}
        for e in events:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        print(f"events harvested: {len(events)} {kinds}", file=sys.stderr)

    tasks = extract_tasks(events, args.bucket_min, project)

    # Project filter (Bug 1: permissive — keep tasks with ANY signal in proj)
    if not args.all_projects:
        scoped = [t for t in tasks if task_in_project(t, project)]
        # #285 never-empty first-run seed: a fresh adopter's new repo has zero
        # in-project tasks, so strict scoping comes up blank — silently breaking
        # VISION's "see your last week" promise. When asked to seed, fall back to
        # the unfiltered cross-project tasks for this first fill, loudly (VISION
        # §4: no silent caps). Existing projects keep their own tasks, so this
        # never fires for them.
        if not scoped and tasks and args.seed_cross_project_if_empty:
            print("⚠ #285 seed: no tasks in this project yet — seeding from "
                  "recent cross-project history so day one isn't blank.",
                  file=sys.stderr)
        else:
            tasks = scoped

    tasks.sort(key=lambda t: t["ts_start"], reverse=True)
    total_found = len(tasks)
    tasks = tasks[: args.max_tasks]
    tasks.sort(key=lambda t: t["ts_start"])  # chronological for streaming

    # No silent caps (VISION §4): if we dropped tasks, SAY so — on stderr and in
    # the JSON — so the board never reads as "covered everything" when it didn't.
    dropped = total_found - len(tasks)
    if dropped > 0:
        print(f"⚠ harvested {len(tasks)} of {total_found} task(s) in the last "
              f"{args.days}d; {dropped} older one(s) not shown — re-run with "
              f"--max-tasks {total_found} or a larger --days to include them.",
              file=sys.stderr)

    out = {
        "project": str(project),
        "windowDays": args.days,
        "bucketMin": args.bucket_min,
        "convoDir": str(convo_dir) if convo_dir else None,
        "taskCount": len(tasks),
        "tasksFound": total_found,
        "tasksDropped": dropped,
        "tasks": [task_to_record(t, project) for t in tasks],
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
