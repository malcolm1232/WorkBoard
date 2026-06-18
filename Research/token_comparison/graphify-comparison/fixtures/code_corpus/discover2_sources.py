#!/usr/bin/env python3
"""discover2 shared event types + source harvesters — extracted from discover2.py (#307 file-split).

The Event shape helpers (parse_ts / msg_text / files_from_tool_use) and every
harvest_* function (jsonl / memory / plans / history / convo / git). A clean
leaf — no heuristics, no task extraction, no config. Re-exported by discover2
so `from discover2 import harvest_jsonl, parse_ts, ...` (e.g. hourly_extractor)
keeps working unchanged.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------- shared types ----------
# Event = (ts:datetime, source:str, kind:str, text:str, files:list[str], meta:dict)
# kind ∈ {"user_prompt", "asst_msg", "tool_use", "memory_write", "convo_user",
#         "convo_asst", "plan_write", "git_commit"}


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        s = s.rstrip("Z") + "+00:00" if s.endswith("Z") else s
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return None


def msg_text(o: dict) -> str:
    msg = o.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts)
    return ""


def files_from_tool_use(o: dict) -> list[tuple[str, str]]:
    """[(tool_name, path), ...] for file-touching tools."""
    out: list[tuple[str, str]] = []
    msg = o.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return out
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        name = c.get("name", "")
        if name not in ("Edit", "Write", "Read", "NotebookEdit", "MultiEdit"):
            continue
        inp = c.get("input") or {}
        p = inp.get("file_path") or inp.get("notebook_path")
        if p:
            out.append((name, p))
    return out


# Review-coverage backfill (#599): an explicit review SKILL (/code-review etc.)
# is a `Skill` tool_use in the transcript — NOT in git — so mining it here lets
# the #598 ledger be accurate from install even for no-git users. Mirrors
# card_commands._REVIEW_SKILLS; ambient code-reading is NOT a review.
REVIEW_SKILLS = {"code-review", "security-review", "simplify", "review", "ultrareview"}


def review_skills_from_tool_use(o: dict) -> list[str]:
    """Names of review SKILLS invoked in this assistant turn (namespace-stripped).
    A `Skill` tool_use whose input.skill (e.g. 'plugin:code-review') resolves to a
    name in REVIEW_SKILLS. [] when none — files_from_tool_use drops Skill, so this
    is the dedicated detector."""
    out: list[str] = []
    msg = o.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return out
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        if c.get("name") != "Skill":
            continue
        skill = ((c.get("input") or {}).get("skill") or "").split(":")[-1].strip()
        if skill in REVIEW_SKILLS:
            out.append(skill)
    return out


# ---------- sources ----------

def harvest_jsonl(since: datetime | None) -> list[dict]:
    """Walk ~/.claude/projects/*/*.jsonl. Yield one event per turn."""
    root = Path.home() / ".claude" / "projects"
    events: list[dict] = []
    if not root.is_dir():
        return events
    for jpath in sorted(root.glob("*/*.jsonl")):
        try:
            with jpath.open("r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    ts = parse_ts(o.get("timestamp"))
                    if ts is None:
                        continue
                    if since and ts < since:
                        continue
                    cwd = o.get("cwd") or ""
                    tp = o.get("type")
                    if tp == "user":
                        txt = msg_text(o).strip()
                        if not txt:
                            continue
                        events.append({
                            "ts": ts, "source": "jsonl", "kind": "user_prompt",
                            "text": txt,
                            "files": [],
                            "meta": {"sessionId": jpath.stem, "cwd": cwd},
                        })
                    elif tp == "assistant":
                        txt = msg_text(o)
                        tu = files_from_tool_use(o)
                        events.append({
                            "ts": ts, "source": "jsonl", "kind": "asst_msg",
                            "text": txt[:2000],
                            "files": [p for _, p in tu],
                            "meta": {"sessionId": jpath.stem, "cwd": cwd,
                                     "tools": [n for n, _ in tu],
                                     "review_skills": review_skills_from_tool_use(o)},
                        })
        except OSError:
            continue
    return events


def _memory_summary(p: Path) -> str:
    """Filename + the frontmatter `description:` + first body paragraph, capped.
    The content (not just the filename) is the richest context on the box (#286)."""
    try:
        txt = p.read_text(errors="replace")
    except OSError:
        return p.name
    desc = ""
    m = re.search(r"^description:\s*(.+)$", txt, re.M)
    if m:
        desc = m.group(1).strip()
    body = re.sub(r"(?s)\A\s*---.*?---\s*", "", txt).strip()   # drop frontmatter
    first_para = body.split("\n\n", 1)[0].strip() if body else ""
    summary = " — ".join(x for x in (desc, first_para) if x)
    return (f"{p.name}: {summary}" if summary else p.name)[:500]


def harvest_memory(since: datetime | None) -> list[dict]:
    """Auto-memory across ALL project slugs (#280) with content read in (#286).
    Memory lives at ~/.claude/projects/<slug>/memory/*.md where <slug> is the
    cwd path of the session that wrote it — so glob every slug, never hardcode
    one ('-Users-malco' was this machine's HOME slug only)."""
    out: list[dict] = []
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return out
    for p in root.glob("*/memory/*.md"):
        try:
            ts = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if since and ts < since:
            continue
        out.append({
            "ts": ts, "source": "memory", "kind": "memory_write",
            "text": _memory_summary(p), "files": [str(p)], "meta": {},
        })
    return out


def harvest_plans(since: datetime | None) -> list[dict]:
    """plans/*.md — mtime signal."""
    out: list[dict] = []
    plan_dir = Path.home() / ".claude" / "plans"
    if not plan_dir.is_dir():
        return out
    for p in plan_dir.glob("*.md"):
        try:
            ts = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if since and ts < since:
            continue
        out.append({
            "ts": ts, "source": "plans", "kind": "plan_write",
            "text": p.name, "files": [str(p)], "meta": {},
        })
    return out


def harvest_history(since: datetime | None,
                    exclude_sessions: set[str] | None = None) -> list[dict]:
    """~/.claude/history.jsonl — the all-projects typed-prompt chronicle (#282).
    Each record is {display, project(=cwd), timestamp(ms), sessionId}. It's a
    SUBSET of harvest_jsonl (prompts only, no assistant turns/tools/files) and
    stores raw display text that dedupes poorly against jsonl, so it's used as a
    TRUE GAP-FILL: skip any sessionId already covered by harvest_jsonl
    (exclude_sessions) — what's left is prompts from sessions whose per-project
    *.jsonl was rotated/pruned. Slash-commands and shell bootstrap lines are
    dropped (never work prompts). cwd is carried so the project filter works."""
    out: list[dict] = []
    exclude_sessions = exclude_sessions or set()
    hist = Path.home() / ".claude" / "history.jsonl"
    if not hist.is_file():
        return out
    try:
        text = hist.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if (o.get("sessionId") or "") in exclude_sessions:
            continue   # jsonl already has this session — not a gap
        disp = (o.get("display") or "").strip()
        # Drop slash-commands (/init, /clear) and bare shell/bootstrap lines —
        # never substantive work prompts.
        if not disp or disp.startswith("/") or disp.startswith("!") or \
           disp.startswith("cd ") or disp.startswith("claude "):
            continue
        raw = o.get("timestamp")
        try:
            ts = datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            continue
        if since and ts < since:
            continue
        out.append({
            "ts": ts, "source": "history", "kind": "user_prompt",
            "text": disp[:2000], "files": [],
            "meta": {"cwd": o.get("project") or "",
                     "sessionId": o.get("sessionId") or "", "fromHistory": True},
        })
    return out


_CONVO_HEADER_RE = re.compile(r"^\[(USER|CLAUDE)\]\s+(\d{1,2}):(\d{2})", re.M)


def harvest_convo(since: datetime | None, convo_dir: Path | None = None) -> list[dict]:
    """conversation_raw_YYMMDD.md — parses [USER] HH:MM markers. convo_dir must
    be resolved by the caller (find_convo_dir); no hardcoded path. None → [].

    Deliberately reads ONLY the _raw_ SUMMARY dumps, not conversation_verbatim_*.md
    (#287, decision A 2026-05-29): the verbatim dumps are the *.jsonl transcripts
    re-rendered to markdown, and we already harvest those raw via harvest_jsonl —
    so parsing verbatim would double-count. The _raw_ summaries are kept because
    they carry the user's curated session structure that the jsonl lacks; the
    cross-source dedupe in _flatten_events drops any that overlap jsonl turns."""
    out: list[dict] = []
    if convo_dir is None or not convo_dir.is_dir():
        return out
    for p in sorted(convo_dir.glob("conversation_raw_*.md")):
        m = re.search(r"conversation_raw_(\d{6})\.md$", p.name)
        if not m:
            continue
        try:
            date_part = datetime.strptime(m.group(1), "%y%m%d")
        except ValueError:
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        # Parse [USER] HH:MM and [CLAUDE] HH:MM markers, anchoring each marker
        # to a UTC ts so it can bucket like jsonl events. The HH:MM is local
        # time — we attach date_part + UTC to keep it timezone-stable.
        for hm in _CONVO_HEADER_RE.finditer(text):
            who, hh, mm = hm.group(1), int(hm.group(2)), int(hm.group(3))
            ts = date_part.replace(hour=hh, minute=mm, tzinfo=timezone.utc)
            if since and ts < since:
                continue
            # Grab the body up to the next header
            body_start = hm.end()
            nm = _CONVO_HEADER_RE.search(text, body_start)
            body = text[body_start:nm.start() if nm else None].strip()
            out.append({
                "ts": ts, "source": "convo",
                "kind": "convo_user" if who == "USER" else "convo_asst",
                "text": body[:2000], "files": [],
                "meta": {"file": str(p)},
            })
    return out


def harvest_git(project: Path, since: datetime | None) -> list[dict]:
    """git log --since=... in project — one event per commit."""
    out: list[dict] = []
    if not (project / ".git").is_dir():
        return out
    since_arg = since.strftime("%Y-%m-%d") if since else "30 days ago"
    try:
        proc = subprocess.run(
            ["git", "-C", str(project), "log",
             f"--since={since_arg}",
             "--pretty=format:%H|%cI|%s"],
            capture_output=True, text=True, timeout=8,
        )
        if proc.returncode != 0:
            return out
        for line in proc.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            sha, iso, subj = parts
            ts = parse_ts(iso)
            if ts is None:
                continue
            if since and ts < since:
                continue
            out.append({
                "ts": ts, "source": "git", "kind": "git_commit",
                "text": subj, "files": [],
                "meta": {"sha": sha, "shaShort": sha[:7]},
            })
    except (OSError, subprocess.SubprocessError):
        pass
    return out



__all__ = [
    "parse_ts", "msg_text", "files_from_tool_use",
    "REVIEW_SKILLS", "review_skills_from_tool_use",
    "harvest_jsonl", "harvest_memory", "harvest_plans", "harvest_history",
    "harvest_convo", "harvest_git",
]
