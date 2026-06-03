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


# ---------- project enumeration (#375) ----------

def _human_ago(ts: datetime, now: datetime) -> str:
    secs = max(0, (now - ts).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


# cwds that are never a "project" — we don't offer a board for the home dir,
# the filesystem root, or the bare Desktop (those are launch dirs, not work).
_NON_PROJECT_DIRS = {
    str(Path.home()),
    str(Path.home() / "Desktop"),
    "/",
}
# Whole subtrees that are scratch / demo / sandbox / tooling, never a real
# project (the /tmp demo runs and ~/.claude meta-work were polluting the
# picker). Prefix match.
_NON_PROJECT_PREFIXES = ("/tmp", "/private/tmp", "/var/folders",
                         str(Path.home() / ".claude"),
                         str(Path.home() / ".board-steward"))


def _is_non_project(cwd: str) -> bool:
    if cwd in _NON_PROJECT_DIRS:
        return True
    return any(cwd == p or cwd.startswith(p.rstrip("/") + "/")
               for p in _NON_PROJECT_PREFIXES)


def _substance_score(n_sessions: int, n_prompts: int, n_edits: int,
                     last_ts: datetime, now: datetime) -> float:
    """Rank a project by how much REAL work happened there, recency-weighted
    (#375). Deliberately transparent (emitted in the record — VISION §4 'no
    hidden magic'): sessions are the strongest 'this is a recurring project'
    signal, edits show depth, prompts show engagement, and recency breaks ties
    toward what was touched lately. A one-off junk session (1 session, a couple
    prompts, no edits, stale) scores near zero and sinks below the top-5."""
    days = (now - last_ts).total_seconds() / 86400.0
    recency = 1.0 / (1.0 + max(0.0, days))      # 1.0 today → 0.5 at 1d → decays
    return round(n_sessions * 10 + n_edits * 2 + n_prompts * 1 + recency * 15, 2)


def list_projects(since: datetime | None,
                  include_home: bool = False) -> list[dict]:
    """Enumerate the project working-dirs the user actually worked in, ranked by
    SUBSTANCE (#375), not bare recency. Uses the SAME signal the task extractor
    uses — the `cwd` recorded on every harvested session turn (harvest_jsonl +
    harvest_history) — NOT a filesystem walk of $HOME and NOT git-root
    resolution. Historical session cwds are meaningful even when the installer's
    launch-cwd is $HOME: the user cd's into a real repo to work, so that repo's
    path is what each turn records.

    A 'project' here is a distinct cwd. Subdir cwds of one repo may appear as
    separate entries — acceptable for a single-pick list; resolving them to a
    common root is the explicitly-deferred git-walk we chose not to build.
    $HOME / Desktop / "/" are excluded (launch dirs, not work) unless
    include_home. Returns ALL matching projects sorted best-first; callers slice
    the top-N for the picker."""
    events = harvest_jsonl(since)
    seen = {(e.get("meta") or {}).get("sessionId") for e in events}
    events.extend(harvest_history(since, exclude_sessions=seen))  # gap-fill
    now = datetime.now(timezone.utc)
    agg: dict[str, dict] = {}
    for e in events:
        kind = e["kind"]
        if kind not in ("user_prompt", "asst_msg"):
            continue
        cwd = (e.get("meta") or {}).get("cwd") or ""
        if not cwd:
            continue
        a = agg.get(cwd)
        if a is None:
            a = {"cwd": cwd, "n_prompts": 0, "n_edits": 0,
                 "sessions": set(), "last_ts": e["ts"]}
            agg[cwd] = a
        if kind == "user_prompt":
            a["n_prompts"] += 1
        elif e.get("files"):        # asst turn that touched files = real work
            a["n_edits"] += 1
        sid = (e.get("meta") or {}).get("sessionId")
        if sid:
            a["sessions"].add(sid)
        if e["ts"] > a["last_ts"]:
            a["last_ts"] = e["ts"]

    # Drop non-project launch dirs FIRST so they can't become a fold target
    # (else WorkBoard would fold into ~/Desktop).
    if not include_home:
        for k in [k for k in agg if _is_non_project(k)]:
            del agg[k]

    # Fold child cwds into their nearest tracked ancestor (#375 picker de-noise):
    # you cd into subdirs of one repo, so WorkBoard/scripts should count toward
    # WorkBoard — not list as a separate "project". Pure path-nesting, NOT a
    # git-walk. Sessions are UNION-merged (a session spanning parent+child must
    # not double-count); prompts/edits sum; last_ts takes the max.
    keys = list(agg.keys())

    def _nearest_ancestor(c: str) -> str | None:
        best = None
        for other in keys:
            if other != c and c.startswith(other.rstrip("/") + "/"):
                if best is None or len(other) > len(best):
                    best = other
        return best

    root_of: dict[str, str] = {}
    for c in keys:
        cur, guard = c, 0
        while guard < 64:
            p = _nearest_ancestor(cur)
            if p is None:
                break
            cur, guard = p, guard + 1
        root_of[c] = cur
    for c in keys:
        r = root_of[c]
        if r == c or r not in agg or c not in agg:
            continue
        src, dst = agg[c], agg[r]
        dst["n_prompts"] += src["n_prompts"]
        dst["n_edits"] += src["n_edits"]
        dst["sessions"] |= src["sessions"]
        if src["last_ts"] > dst["last_ts"]:
            dst["last_ts"] = src["last_ts"]
        del agg[c]

    rows = []
    for r in agg.values():
        score = _substance_score(len(r["sessions"]), r["n_prompts"],
                                 r["n_edits"], r["last_ts"], now)
        rows.append({
            "project": r["cwd"],
            "name": Path(r["cwd"]).name or r["cwd"],
            "last_activity": r["last_ts"].isoformat(),
            "ago": _human_ago(r["last_ts"], now),
            "n_prompts": r["n_prompts"],
            "n_edits": r["n_edits"],
            "n_sessions": len(r["sessions"]),
            "score": score,
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


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
    ap.add_argument("--list-projects", action="store_true",
                    help="#375: enumerate distinct project cwds from session "
                         "history (ranked by recency) instead of extracting "
                         "tasks. For the install-time project picker.")
    ap.add_argument("--top", type=int, default=5,
                    help="--list-projects: show only the top-N most SUBSTANTIAL "
                         "projects (default 5; 0 = all). The remaining count is "
                         "reported as 'more' so the picker can offer to expand.")
    ap.add_argument("--format", choices=("json", "lines"), default="json",
                    help="--list-projects output: json (default) or tab-"
                         "separated 'path<TAB>label' lines for shell pickers.")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)
             if args.days > 0 else None)

    if args.list_projects:
        rows = list_projects(since)
        total = len(rows)
        shown = rows[: args.top] if args.top and args.top > 0 else rows
        more = max(0, total - len(shown))
        if args.format == "lines":
            for r in shown:
                label = (f"{r['name']} — {r['project']}  "
                         f"({r['ago']}, {r['n_sessions']} session"
                         f"{'s' if r['n_sessions'] != 1 else ''}, "
                         f"{r['n_edits']} edits)")
                sys.stdout.write(f"{r['project']}\t{label}\n")
            if more > 0:
                sys.stdout.write(f"\t… {more} more not shown "
                                 f"(re-run with --top 0 for all)\n")
        else:
            json.dump({"projects": shown, "total": total,
                       "shown": len(shown), "more": more},
                      sys.stdout, indent=2, ensure_ascii=False, default=str)
            sys.stdout.write("\n")
        return

    if args.legacy:
        # discover.py was archived to dev/ (#deadweight cleanup); still runnable
        # via this off-by-default flag for anyone who wants the old session shape.
        legacy = Path(__file__).resolve().parent.parent / "dev" / "discover.py"
        os.execvp(sys.executable, [sys.executable, str(legacy),
                                   "--project", str(args.project),
                                   "--days", str(args.days)])
        return  # not reached

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
