#!/usr/bin/env python3
"""board-steward command implementations — extracted from card.py (#307 file-split, 3-way).

Every cmd_* function (add/update/move/fly/improve/bug/auto-ship/sim/subtask/
link/column/show/recover/migrate/repair-links/prelaunch-check/list/digest/
query/wiki/metrics/export) plus their private helpers. The shared toolkit
(load/atomic_save/find_card/...) comes from card_state. build_parser()/main()
live in card.py, the CLI entry point.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
import _boardio  # noqa: E402
import _render   # noqa: E402  (shared markdown/html renderers — #115 export/wiki)
import _metrics  # noqa: E402  (velocity metrics — #114)

from card_state import *  # noqa: E402,F401,F403  (shared toolkit)


# ===== commands =====

def cmd_add(args, d, board):
    if args.id:
        if any(c.get("id") == args.id for c in d["cards"]):
            sys.exit(f"error: id '{args.id}' already exists")
        cid = args.id
    else:
        slug = slugify(args.code or args.title)
        cid = f"c-{slug}"
        # disambiguate if needed
        if any(c.get("id") == cid for c in d["cards"]):
            n = 2
            while any(c.get("id") == f"{cid}-{n}" for c in d["cards"]):
                n += 1
            cid = f"{cid}-{n}"

    origin = maybe_stdin(args.origin, args.origin_stdin) or ""
    notes  = maybe_stdin(args.notes, args.notes_stdin)   or ""
    writeup= maybe_stdin(args.writeup, args.writeup_stdin) or ""

    now = now_iso()
    # --created-at override: use the provided ISO ts as createdAt (and doneAt
    # for cards landing directly in done). updatedAt stays = now since the
    # card row was actually written now.
    created = getattr(args, "created_at", None) or now
    tags = _check_tags(args.tag or [], d, getattr(args, "force", False))

    # Provenance (#385): a --created-at means this card was BACKFILLED from mined
    # history (bootstrap/hourly harvest), not live work — i.e. a "discovered"
    # card. The reconciliation sweep (hourly_reconcile.reconcile_sweep) only
    # considers cards tagged 'discovered', so WITHOUT this stamp a freshly
    # bootstrapped board is invisible to recon and stale IP cards never get
    # reconciled. Stamp it at the single emit chokepoint (not in per-path Haiku/
    # inline prompts that can forget). Live cards have no --created-at, so they
    # stay untagged and recon never auto-moves user-authored work — preserving
    # the tag's original safety purpose. Appended directly (bypasses taxonomy
    # validation) since 'discovered' is a provenance marker, not a taxonomy tag.
    if getattr(args, "created_at", None) and "discovered" not in tags:
        tags = tags + ["discovered"]

    # Auto-urgent (#85): detect urgency keywords in title+origin and route to
    # the SUPER URGENT column with critical priority. --urgent forces; --no-auto-urgent skips.
    auto_urgent_kw = None
    auto_urgent_col_created = False
    if getattr(args, "urgent", False):
        auto_urgent_kw = "--urgent"
    elif not getattr(args, "no_auto_urgent", False):
        auto_urgent_kw = _detect_urgency(args.title, origin)
    target_col = args.column
    target_prio = args.priority
    if auto_urgent_kw:
        auto_urgent_col_created = _ensure_super_urgent_col(d)
        target_col = "super-urgent"
        if target_prio not in ("critical",):
            target_prio = "critical"

    # Auto-card (#100): --auto signals intent-detected creation. Defaults the
    # column to 'ideas' when caller didn't override, ensures the col exists,
    # and stamps meta.autoCreated so board.html can pop an undo toast.
    auto_card = bool(getattr(args, "auto", False))
    auto_card_col_created = False
    if auto_card and not auto_urgent_kw:
        if args.column == "backlog":  # caller didn't override the default
            target_col = "ideas"
        if target_col == "ideas":
            auto_card_col_created = _ensure_ideas_col(d)

    card = {
        "num": d["nextNum"],
        "id": cid,
        "code": args.code or "",
        "priority": target_prio,
        "title": args.title,
        "column": target_col,
        "tags": tags,
        "origin": origin,
        "notes": notes,
        "writeup": writeup,
        "createdAt": created,
        "updatedAt": now,
        "doneAt": created if target_col == "done" else None,
        "lastTouchedSubtask": None,
        "linkedCards": [],
        "subtasks": [],
    }
    if auto_card:
        card["meta"] = {
            "autoCreated": True,
            "autoSource": (getattr(args, "auto_source", None) or "").strip(),
        }
    d["cards"].append(card)
    d["nextNum"] += 1

    for target_ref in (args.link or []):
        other = find_card(d, target_ref)
        if other["id"] != cid:
            card["linkedCards"].append(other["id"])
            other.setdefault("linkedCards", [])
            if cid not in other["linkedCards"]:
                other["linkedCards"].append(cid)
            other["updatedAt"] = now

    # #254 — a card born directly into In Progress is active work too (parity
    # with the UI's handleCardAdded → applyActiveWorkTransition).
    _set_active_work(d, card, "", target_col)
    _record_move(card, None, target_col)
    rev = atomic_save(board, d)
    if auto_urgent_kw:
        _log_auto_urgent(board, card["num"], auto_urgent_kw, auto_urgent_col_created)
        col_note = " (🚨 col created)" if auto_urgent_col_created else ""
        print(f"+ #{card['num']} {card['code'] or card['id']} → {target_col}{col_note} "
              f"[auto-urgent: '{auto_urgent_kw}'] (rev {rev})")
    elif auto_card:
        col_note = " (💡 col created)" if auto_card_col_created else ""
        src = (getattr(args, "auto_source", None) or "").strip()
        src_note = f" [auto-card: '{src}']" if src else " [auto-card]"
        print(f"+ #{card['num']} {card['code'] or card['id']} → {target_col}{col_note}"
              f"{src_note} (rev {rev})")
    else:
        print(f"+ #{card['num']} {card['code'] or card['id']} → {target_col} (rev {rev})")


def cmd_update(args, d, board):
    c = find_card(d, args.ref)
    changed = []
    if args.title is not None:    c["title"] = args.title;       changed.append("title")
    if args.code is not None:     c["code"] = args.code;         changed.append("code")
    if args.priority is not None: c["priority"] = args.priority; changed.append("priority")
    if args.column is not None:
        c["column"] = args.column
        if args.column == "done" and not c.get("doneAt"):
            c["doneAt"] = now_iso()
        changed.append("column")

    for field, lit, sflag in [("origin", args.origin, args.origin_stdin),
                              ("notes", args.notes, args.notes_stdin),
                              ("writeup", args.writeup, args.writeup_stdin)]:
        v = maybe_stdin(lit, sflag)
        if v is not None:
            c[field] = v
            changed.append(field)

    for t in _check_tags(args.add_tag or [], d, getattr(args, "force", False)):
        c.setdefault("tags", [])
        if t not in c["tags"]:
            c["tags"].append(t)
            changed.append(f"+tag:{t}")
    for t in (args.rm_tag or []):
        if t in c.get("tags", []):
            c["tags"].remove(t)
            changed.append(f"-tag:{t}")

    # #102 BOARD-AUTO-LINK — linkedFiles drive the PreToolUse flash hook.
    # Paths are normalised to absolute form so basename + absolute hits both
    # work against the same canonical entry.
    for fp in (getattr(args, "add_linked_file", None) or []):
        fp_abs = str(Path(fp).expanduser().resolve())
        c.setdefault("linkedFiles", [])
        if fp_abs not in c["linkedFiles"]:
            c["linkedFiles"].append(fp_abs)
            changed.append(f"+file:{Path(fp_abs).name}")
    for fp in (getattr(args, "rm_linked_file", None) or []):
        fp_abs = str(Path(fp).expanduser().resolve())
        before = list(c.get("linkedFiles") or [])
        c["linkedFiles"] = [x for x in before if x != fp_abs]
        if len(c["linkedFiles"]) != len(before):
            changed.append(f"-file:{Path(fp_abs).name}")

    if not changed:
        sys.exit("nothing to update — pass at least one field")
    c["updatedAt"] = now_iso()
    rev = atomic_save(board, d)
    print(f"~ #{c['num']} {','.join(changed)} (rev {rev})")


def _autocheck_subtasks(nodes, ts):
    """Recursively mark all open subtasks done (with doneAt=ts)."""
    for st in nodes:
        if not st.get("done"):
            st["done"] = True
            st["doneAt"] = ts
        if st.get("children"):
            _autocheck_subtasks(st["children"], ts)


def _find_subtask_anywhere(nodes, sid):
    """Locate subtask by id in the tree; return the node or None."""
    for st in nodes:
        if st.get("id") == sid:
            return st
        kid = _find_subtask_anywhere(st.get("children") or [], sid)
        if kid:
            return kid
    return None


def _set_active_work(d, card, old_col, new_col):
    """#254 — track the single active-work card = the last one MOVED INTO
    In Progress. Persisted in board.json as activeWorkId so the pulse + top-pin
    survive a page refresh. A card merely SITTING in In Progress never becomes
    active (only a real transition does); moving the active card out clears it.
    This is the old live-transition definition, made persistent."""
    if new_col == "inprogress" and old_col != "inprogress":
        # #546 — the card that most recently TRANSITIONED into In-Progress becomes
        # the active (pulsing) one and sticks to that card (by id) until the NEXT
        # card transitions into IP and takes over. (Reverts the #503 "don't steal".)
        d["activeWorkId"] = card["id"]
    elif old_col == "inprogress" and new_col != "inprogress" \
            and d.get("activeWorkId") == card["id"]:
        d["activeWorkId"] = None


def _record_move(card, old_col, new_col, via=None):
    """#258 — append a movement event {from, to, at} to the card's history so
    every column shift is timestamped. A clean structured label timeline for the
    transition-prediction model (#251) — lives in board.json, no JSONL parsing.
    Creation is recorded with from=None. No-op when the column doesn't change.

    #504 — optional ``via`` tags WHO moved the card (recon | undo | harvest |
    autoship). Plain user/agent moves leave it unset. The Logs HUD reads the
    latest event's via to prefix the line, e.g. `(Recon) MOVE #12 …`, so an
    automated move is never mistaken for a hands-on one."""
    if old_col == new_col:
        return
    # #509 — a CLI move IS the agent (Claude/automation). Default via to
    # 'agent' so the Logs HUD shows (Agent) MOVE, distinct from a human's
    # (User) browser drag. The specific automated sources (recon | undo |
    # harvest | autoship) pass their own via and override this default.
    ev = {"from": old_col, "to": new_col, "at": now_iso(), "via": via or "agent"}
    card.setdefault("history", []).append(ev)


def _looks_multipart(title: str, origin: str) -> bool:
    """Heuristic: does this card describe MORE THAN ONE part? Used by the
    decompose-before-IP guard (#103). Conservative — only fires on strong
    list signals so a single task with one stray comma doesn't trip it:
      • a ` + `-joined title (the canonical multi-part shape, e.g.
        `Column delete + grip drag + drag-to-trash`)
      • a numbered list with ≥2 items (`1. … 2. …` / `1) … 2)`)
      • `Header: a, b` — a colon followed by a comma-list
      • ≥2 commas in the title (a list of ≥3 things)
      • an explicit `, and …` / `; …` list joiner anywhere
    """
    title = title or ""
    origin = origin or ""
    text = f"{title}\n{origin}".lower()
    if " + " in title:   # +-joined part labels — the canonical 2a title format
        return True
    nums = re.findall(r"(?:^|\s)(\d+)[.)]\s", text)
    if len(set(nums)) >= 2:
        return True
    if ":" in title and "," in title.split(":", 1)[1]:
        return True
    if title.count(",") >= 2:
        return True
    if re.search(r",\s+and\s+\S", text) or re.search(r";\s+\S", text):
        return True
    return False


def cmd_fly(args, d, board):
    """FLY transition — atomic single-hop column change with side-effect
    shortcuts and a built-in animation pause so chained flies don't race
    the browser's simulateUserDragMove (~320ms).

    The ONLY column-change verb. (`move` was removed — it mutated data with
    no animation contract, so cards jumped; every transition now flies.)

    Side-effect flags (apply BEFORE the hop):
      --bug REASON     → add 'bug' tag + 🐞 fix-bug subtask
      --improve TEXT   → add improvement subtask
      --subtask TEXT   → add plain subtask
      --note TEXT      → append to notes
      --writeup TEXT   → set writeup (typical for hops into 'done')

    Pause:
      --pause-ms N     → sleep N ms AFTER the save (default 400)
    """
    c = find_card(d, args.ref)
    old = c["column"]
    ts = now_iso()

    # Tolerate Namespaces built by internal callers (sim / auto-ship / recover)
    # that only set ref/column/writeup — every side-effect flag is optional.
    note     = getattr(args, "note", None)
    subtask  = getattr(args, "subtask", None)
    bug       = getattr(args, "bug", None)
    improve  = getattr(args, "improve", None)
    via      = getattr(args, "via", None)  # #504 — who moved it (recon|undo|…)
    pause_ms = getattr(args, "pause_ms", 0)
    writeup  = maybe_stdin(getattr(args, "writeup", None),
                           getattr(args, "writeup_stdin", False))

    # Side-effects in order: note, plain subtask, bug, improve.
    if note:
        existing = (c.get("notes") or "").rstrip()
        c["notes"] = (existing + "\n" + note) if existing else note
    if subtask:
        c.setdefault("subtasks", [])
        sid = new_subtask_id(c)
        c["subtasks"].append({
            "id": sid, "text": subtask, "done": False,
            "createdAt": ts, "children": [],
        })
        c["lastTouchedSubtask"] = sid
    if bug:
        c.setdefault("tags", [])
        if "bug" not in c["tags"]:
            c["tags"].append("bug")
        c.setdefault("subtasks", [])
        sid = new_subtask_id(c)
        reason = (bug or "").strip()
        text = f"🐞 fix bug: {reason}" if reason else "🐞 fix bug"
        c["subtasks"].append({
            "id": sid, "text": text, "done": False,
            "createdAt": ts, "children": [],
        })
        c["lastTouchedSubtask"] = sid
    if improve:
        c.setdefault("subtasks", [])
        sid = new_subtask_id(c)
        c["subtasks"].append({
            "id": sid, "text": improve, "done": False,
            "createdAt": ts, "children": [],
        })
        c["lastTouchedSubtask"] = sid

    # #107 PHASE-CARD GUARD — a phase card is a roadmap CONTAINER, not a unit of
    # work; it must never become the active pulse (it would sit in IP for the
    # whole phase). On the start-work hop, block it and tell the agent to
    # GRADUATE the specific deliverable into its own linked card. Phase detection:
    # the structural `phase` tag (primary) or a "Phase <n>" title (law format).
    _is_phase = ("phase" in (c.get("tags") or [])
                 or bool(re.match(r"\s*phase\s*\d", c.get("title", ""), re.I)))
    if (args.column == "inprogress" and old in ("task", "backlog")
            and not bug and not improve
            and not getattr(args, "force", False)
            and os.environ.get("BOARD_SKIP_DECOMPOSE_CHECK") != "1"
            and _is_phase):
        sys.exit(
            f"✋ #{c['num']} is a PHASE card — phases don't go in-progress (too big "
            f"for one pulse). GRADUATE the deliverable you're starting into its own card:\n"
            f"    card.py add --column task --title \"<deliverable>\" --link {c['num']}\n"
            f"    card.py fly <new#> inprogress\n"
            f"  then tick this phase's subtask when that deliverable card ships.\n"
            f"  (genuinely want the whole phase in IP? add --force)"
        )

    # #103 DECOMPOSE-BEFORE-IP GUARD — block a multi-part card from reaching
    # inprogress with zero subtasks (the scenario-#2 failure: parts get lost,
    # the card lands in IP showing only the auto 1/1). Narrowly scoped to the
    # genuine "start work" hop (task/backlog → inprogress), never the --bug/
    # --improve reopen flows. A same-call --subtask already added one above, so
    # this only fires on a truly naked card. Override: --force, or env
    # BOARD_SKIP_DECOMPOSE_CHECK=1 for automation (reconcile/e2e).
    if (args.column == "inprogress" and old in ("task", "backlog")
            and not bug and not improve
            and not getattr(args, "force", False)
            and os.environ.get("BOARD_SKIP_DECOMPOSE_CHECK") != "1"
            and not (c.get("subtasks") or [])
            and _looks_multipart(c.get("title", ""), c.get("origin", ""))):
        sys.exit(
            f"✋ #{c['num']} looks MULTI-PART but has no subtasks — decompose "
            f"BEFORE inprogress (carding LAW #103):\n"
            f"    title: {c.get('title','')!r}\n"
            f"  • related parts of ONE deliverable → add them first:\n"
            f"      card.py subtask add {c['num']} \"<part>\"   (then re-run fly)\n"
            f"  • INDEPENDENT tasks → make N separate cards instead\n"
            f"  • genuinely one atomic task → card.py fly {c['num']} inprogress --force"
        )

    # #537 ONE-IN-FLIGHT GUARD — keep the live flow SEQUENTIAL (task→IP→done, one
    # at a time) so cards don't pile up in In-Progress and get flown to Done in a
    # batch. Keyed on the ACTIVE (pulsing) card — the one most-recently moved into
    # IP — not every parked card, so old backlog-in-IP doesn't nag. Soft: --force
    # for genuine parallel work; BOARD_SKIP_DECOMPOSE_CHECK exempts automation
    # (reconcile / e2e / sim / replay), same as the guards above.
    if (args.column == "inprogress" and old in ("task", "backlog")
            and not bug and not improve
            and not getattr(args, "force", False)
            and os.environ.get("BOARD_SKIP_DECOMPOSE_CHECK") != "1"):
        _awid = d.get("activeWorkId")
        _active = next((x for x in d.get("cards", []) if x.get("id") == _awid), None) if _awid else None
        if (_active and _active.get("id") != c.get("id")
                and _active.get("column") == "inprogress" and not _active.get("doneAt")):
            sys.exit(
                f"✋ #{_active['num']} is still in progress (the active/pulsing card). "
                f"Keep the flow sequential — finish it first:\n"
                f"    card.py fly {_active['num']} done --writeup \"...\"\n"
                f"  then start #{c['num']}.\n"
                f"  (genuinely working both in parallel? add --force)"
            )

    # #476 DONE-COMPLETENESS GUARD — don't let a card reach Done with unfinished
    # subtasks (the "flew to Done at 1/4, forgot to tick the rest" miss). The
    # done-hop below auto-closes lastTouchedSubtask, so exclude it; the auto
    # '☑ initial ship' is only added when there are NO subtasks, so it's not
    # here for a multi-part card. Override: --force (a deliberate partial
    # "shipped X/N" ship) or BOARD_SKIP_DECOMPOSE_CHECK=1 (automation).
    if (args.column == "done" and old != "done"
            and not getattr(args, "force", False)
            and os.environ.get("BOARD_SKIP_DECOMPOSE_CHECK") != "1"):
        _last = c.get("lastTouchedSubtask")
        def _open_leaves(nodes):
            out = []
            for s in nodes or []:
                kids = s.get("children") or []
                if kids:
                    out += _open_leaves(kids)
                elif not s.get("done") and s.get("id") != _last:
                    out.append(s)
            return out
        _open = _open_leaves(c.get("subtasks"))
        if _open:
            _names = "; ".join((s.get("text") or "")[:34] for s in _open[:3])
            sys.exit(
                f"✋ #{c['num']} has {len(_open)} unfinished subtask(s) — finish before Done:\n"
                f"    {_names}{' …' if len(_open) > 3 else ''}\n"
                f"  • tick each as you go: card.py subtask done {c['num']} <sid>\n"
                f"  • deliberately shipping partial (X/N)? card.py fly {c['num']} done --force"
            )

    # The hop + done-semantics: cycle-history (#188) and bug-tag auto-strip.
    c["column"] = args.column
    if args.column == "super-urgent":   # #104 — ensure the urgent column exists (fly/reconcile target)
        _ensure_super_urgent_col(d)
    if args.column == "done":
        c["doneAt"] = c.get("doneAt") or ts
        if "bug" in (c.get("tags") or []):
            c["tags"] = [t for t in c["tags"] if t != "bug"]
        c.setdefault("subtasks", [])
        if not c["subtasks"]:
            sid = new_subtask_id(c)
            c["subtasks"].append({
                "id": sid, "text": "☑ initial ship",
                "done": True, "doneAt": ts,
                "createdAt": ts, "children": [],
            })
            c["lastTouchedSubtask"] = sid
        else:
            sid = c.get("lastTouchedSubtask")
            st = _find_subtask_anywhere(c["subtasks"], sid) if sid else None
            if st and not st.get("done"):
                st["done"] = True
                st["doneAt"] = ts
    elif old == "done":
        c["doneAt"] = None

    if writeup is not None:
        c["writeup"] = writeup

    c["updatedAt"] = now_iso()
    _set_active_work(d, c, old, args.column)
    _record_move(c, old, args.column, via=via)
    rev = atomic_save(board, d)

    badge = " 🐞" if bug else (" ✨" if improve else "")
    suffix = " + writeup" if writeup is not None else ""
    print(f"✈ #{c['num']} {old} → {args.column}{badge}{suffix} (rev {rev})")

    if pause_ms > 0:
        time.sleep(pause_ms / 1000.0)


# ═════════════════════════════════════════════════════════════════════
# LIFECYCLE — DO NOT BREAK
# Canonical Claude-task lifecycle wrapped as a single command. The
# orchestration here pairs with the browser-side animation contract
# (window.runLifecycle() in board.html). When adding features, run
# `card.py sim` to verify the end-to-end visual is intact.
# ═════════════════════════════════════════════════════════════════════
def cmd_improve(args, d, board):
    """IMPROVE transition. Done → In Progress + add a new subtask.

    The 5th canonical lifecycle verb (see VISION.md §4). Use this when a
    shipped card needs an enhancement (not a regression — for regressions
    use `card.py bug`). The new subtask captures *what's being added*;
    the parent card stays the same card across improvements (VISION.md:
    'subtasks tree out inside the card, parent never leaves').
    """
    c = find_card(d, args.ref)
    old = c["column"]
    c["column"] = "inprogress"
    c["doneAt"] = None
    c.setdefault("subtasks", [])
    sid = new_subtask_id(c)
    c["subtasks"].append({
        "id": sid,
        "text": args.text,
        "done": False,
        "createdAt": now_iso(),
        "children": [],
    })
    c["lastTouchedSubtask"] = sid
    c["updatedAt"] = now_iso()
    _set_active_work(d, c, old, "inprogress")
    _record_move(c, old, "inprogress")
    rev = atomic_save(board, d)
    print(f"✨ #{c['num']} {old} → inprogress (+subtask {sid}) (rev {rev})")


def cmd_bug(args, d, board):
    """REOPEN-AS-BUG transition. Done → In Progress + 'bug' tag + bug
    cycle subtask. The 4th canonical lifecycle verb (see VISION.md §4).

    Same effect as the modal's '🐞 Reopen as bug' button: card moves back
    to In Progress, doneAt clears, 'bug' tag added (idempotent), AND a
    new open subtask "🐞 fix bug[: <reason>]" is appended so the bug
    cycle is first-class history (per #188). The 'bug' tag is auto-
    stripped again when the card next lands in done (regression fixed);
    the bug-cycle subtask gets closed by the next ship and stays as
    permanent evidence "this card had a regression".
    """
    c = find_card(d, args.ref)
    if c["column"] == "inprogress" and "bug" in (c.get("tags") or []):
        sys.exit(f"error: #{c['num']} is already an open bug")
    old = c["column"]
    c["column"] = "inprogress"
    c["doneAt"] = None
    c.setdefault("tags", [])
    if "bug" not in c["tags"]:
        c["tags"].append("bug")
    # #188 — bug cycle = a subtask.
    c.setdefault("subtasks", [])
    sid = new_subtask_id(c)
    reason = (args.reason or "").strip()
    text = f"🐞 fix bug: {reason}" if reason else "🐞 fix bug"
    c["subtasks"].append({
        "id": sid, "text": text,
        "done": False,
        "createdAt": now_iso(), "children": [],
    })
    c["lastTouchedSubtask"] = sid
    c["updatedAt"] = now_iso()
    _set_active_work(d, c, old, "inprogress")
    _record_move(c, old, "inprogress")
    rev = atomic_save(board, d)
    print(f"🐞 #{c['num']} {old} → inprogress (+bug tag, +subtask {sid}) (rev {rev})")


# ===== auto-ship (#101 Phase 3b) =====

def _find_git_root(start: Path) -> Path:
    """Walk up from start looking for a .git dir/file. Returns start if none."""
    p = start.resolve()
    for _ in range(8):
        g = p / ".git"
        if g.is_dir() or g.is_file():
            return p
        if p.parent == p:
            break
        p = p.parent
    return start.resolve()


def _git_log_since(since_ref: str, cwd: Path) -> list[tuple[str, str]]:
    """git log <since_ref>..HEAD → [(short_sha, subject), ...] oldest-first."""
    try:
        out = subprocess.run(
            ["git", "log", f"{since_ref}..HEAD", "--format=%h\t%s", "--reverse"],
            cwd=str(cwd), capture_output=True, text=True, timeout=5, check=True,
        )
    except Exception:
        return []
    rows = []
    for ln in out.stdout.strip().splitlines():
        if "\t" in ln:
            sha, subj = ln.split("\t", 1)
            rows.append((sha.strip(), subj.strip()))
    return rows


def _score_card_against_commits(card: dict, commits: list[tuple[str, str]]) -> tuple[int, list[str]]:
    """Score how strongly a card matches a list of commit subjects.

    Code exact match     = 3 pts
    #num marker          = 2 pts
    long title token     = 1 pt each (token >= 5 chars, max 3 tokens counted)

    Returns (total_score, matched_sha_list). A score >= 2 is treated as a
    confident match (either code+anything, OR #num+anything)."""
    score = 0
    hits: list[str] = []
    code = (card.get("code") or "").upper().strip()
    num_marker = f"#{card.get('num')}"
    title = (card.get("title") or "").lower()
    title_tokens = [w for w in title.split() if len(w) >= 5][:3]
    for sha, subj in commits:
        s = 0
        su = subj.upper()
        sl = subj.lower()
        if code and code in su:
            s += 3
        if num_marker in subj:
            s += 2
        for w in title_tokens:
            if w in sl:
                s += 1
        if s:
            score += s
            hits.append(sha)
    return score, hits


def _auto_ship_writeup(card: dict, commits: list[tuple[str, str]], hits: list[str], extra: str | None) -> str:
    """Build the writeup body for an auto-ship: one-line header naming the
    matched SHAs, then a bullet list of relevant commit subjects, then the
    optional extra prose."""
    relevant = [(s, sub) for s, sub in commits if not hits or s in hits]
    if not relevant:
        relevant = commits[-3:]  # last 3 commits as soft fallback
    sha_list = ", ".join(hits) if hits else relevant[-1][0]
    lines = [f"Shipped in {sha_list}.", ""]
    for sha, subj in relevant:
        lines.append(f"  {sha}  {subj}")
    if extra:
        lines.append("")
        lines.append(extra.strip())
    return "\n".join(lines)


def cmd_auto_ship(args, d, board):
    """#101 BOARD-AUTO-MOVE: auto-promote inprogress cards to done using git log.

    Two modes:
      Scan mode (no ref):  scan inprogress cards, score matches against commits
                           in <since-ref>..HEAD, print candidate table.
      Ship mode (ref):     move that card to done with an auto-generated writeup
                           assembled from the matching commits.

    Default is dry-run preview. Pass --apply to actually move."""
    git_root = _find_git_root(board.parent)
    commits = _git_log_since(args.since_ref, cwd=git_root)
    if not commits:
        sys.exit(f"no commits in range {args.since_ref}..HEAD (git_root={git_root})")

    # SCAN mode
    if not args.ref:
        inprog = [c for c in d["cards"] if c.get("column") == "inprogress"]
        if not inprog:
            print("(no cards in inprogress)")
            return
        rows = []
        for c in inprog:
            score, hits = _score_card_against_commits(c, commits)
            if score >= 2:
                rows.append((c, score, hits))
        rows.sort(key=lambda r: (-r[1], r[0]["num"]))
        print(f"# auto-ship candidates ({args.since_ref}..HEAD, {len(commits)} commits)")
        if not rows:
            print("(no inprogress cards match recent commits)")
            return
        for c, score, hits in rows:
            code = c.get("code") or c.get("id")
            print(f"  #{c['num']:>3} score={score:<2}  {code:<22} {c.get('title','')[:54]}")
            for sha in hits:
                subj = next((s for x, s in commits if x == sha), "")
                print(f"        ↳ {sha}  {subj[:80]}")
        print(f"\n→ to ship one: card.py auto-ship <num> --since-ref {args.since_ref} --apply")
        return

    # SHIP mode
    c = find_card(d, args.ref)
    if c.get("column") == "done" and not args.force:
        sys.exit(f"#{c['num']} already in done (use --force to re-ship)")
    score, hits = _score_card_against_commits(c, commits)
    writeup = _auto_ship_writeup(c, commits, hits, getattr(args, "writeup_extra", None))

    if not args.apply:
        code = c.get("code") or c.get("id")
        print(f"DRY-RUN: would ship #{c['num']} {code} (score={score}, {len(hits)} commit hits)")
        if score < 2:
            print(f"WARN: low match score — no commit in {args.since_ref}..HEAD obviously mentions this card.")
            print("      Re-run with --apply if you've confirmed by eye, or pick a wider --since-ref.")
        print("--- writeup ---")
        print(writeup)
        print("--- end ---")
        print("(re-run with --apply to actually move)")
        return

    # Apply: the canonical done branch (same as cmd_fly).
    old = c["column"]
    c["column"] = "done"
    ts = now_iso()
    c["doneAt"] = c.get("doneAt") or ts
    if "bug" in (c.get("tags") or []):
        c["tags"] = [t for t in c["tags"] if t != "bug"]
    c.setdefault("subtasks", [])
    if not c["subtasks"]:
        sid = new_subtask_id(c)
        c["subtasks"].append({
            "id": sid, "text": "☑ initial ship",
            "done": True, "doneAt": ts,
            "createdAt": ts, "children": [],
        })
        c["lastTouchedSubtask"] = sid
    else:
        sid = c.get("lastTouchedSubtask")
        st = _find_subtask_anywhere(c["subtasks"], sid) if sid else None
        if st and not st.get("done"):
            st["done"] = True
            st["doneAt"] = ts
    c["writeup"] = writeup
    c["updatedAt"] = ts
    _set_active_work(d, c, old, "done")
    _record_move(c, old, "done", via="autoship")
    rev = atomic_save(board, d)
    print(f"✈ #{c['num']} {old} → done [auto-ship, {len(hits)} commit hits] (rev {rev})")


def cmd_sim(args, d, board):
    try:
        gap_ip, gap_done = (float(x) for x in args.intervals.split(","))
    except Exception:
        sys.exit("--intervals must be 'task→ip,ip→done' seconds, e.g. '2,5'")

    title = args.title or f"SIMULATION {now_iso()[11:19].replace(':', '')}"
    writeup = args.writeup or f"Sim via card.py sim (intervals {args.intervals}s)."

    # Step 1 — CREATE in Task. Reuses cmd_add so the lifecycle exercises
    # exactly the production code path (no shortcut writes).
    add_ns = argparse.Namespace(
        title=title, code="", priority=args.priority, column="task",
        tag=[], link=[], id=None,
        origin=None, origin_stdin=False,
        notes=None,  notes_stdin=False,
        writeup=None, writeup_stdin=False,
    )
    cmd_add(add_ns, d, board)
    num = d["nextNum"] - 1

    # Step 2 — FLY to In Progress (5s+ default to watch the pulse).
    time.sleep(gap_ip)
    d = load(board)  # reload in case anything else touched the board
    cmd_fly(argparse.Namespace(
        ref=str(num), column="inprogress",
        writeup=None, writeup_stdin=False, pause_ms=0,
    ), d, board)

    # Step 3 — FLY to Done with auto writeup.
    time.sleep(gap_done)
    d = load(board)
    cmd_fly(argparse.Namespace(
        ref=str(num), column="done",
        writeup=writeup, writeup_stdin=False, pause_ms=0,
    ), d, board)

    # Step 4 (optional) — REOPEN AS BUG: simulate a post-ship regression.
    # Card moves Done → In Progress + gets a 'bugged' tag (visible forever
    # in card history). Then a follow-up move to Done with a fix writeup.
    if args.with_bug:
        time.sleep(gap_done)
        d = load(board)
        # Use the canonical bug verb so the sim exercises the production
        # code path (DO NOT BREAK contract — see VISION.md §4).
        cmd_bug(argparse.Namespace(ref=str(num)), d, board)

        time.sleep(gap_ip)
        d = load(board)
        cmd_fly(argparse.Namespace(
            ref=str(num), column="done",
            writeup=f"Bug fixed and reshipped. {writeup}",
            writeup_stdin=False, pause_ms=0,
        ), d, board)

    print(f"✓ sim complete: #{num}")


def cmd_subtask(args, d, board):
    c = find_card(d, args.ref)
    c.setdefault("subtasks", [])
    touched_sid = None  # the subtask id this op touched — used by #188 ship logic.

    if args.op == "add":
        sid = new_subtask_id(c)
        st = {"id": sid, "text": args.text, "done": False, "collapsed": False, "children": []}
        if args.parent:
            r = find_subtask(c["subtasks"], args.parent)
            if not r:
                sys.exit(f"error: no subtask '{args.parent}' under #{c['num']}")
            r[0].setdefault("children", []).append(st)
        else:
            c["subtasks"].append(st)
        touched_sid = sid
        action = f"+ {sid}: {args.text[:60]}"

    elif args.op in ("done", "undone"):
        r = find_subtask(c["subtasks"], args.sid)
        if not r:
            sys.exit(f"error: no subtask '{args.sid}' under #{c['num']}")
        r[0]["done"] = (args.op == "done")
        touched_sid = args.sid
        action = f"{'✓' if args.op == 'done' else '○'} {args.sid}"

    elif args.op == "rm":
        r = find_subtask(c["subtasks"], args.sid)
        if not r:
            sys.exit(f"error: no subtask '{args.sid}' under #{c['num']}")
        r[1].remove(r[0])
        action = f"- {args.sid}"

    else:
        sys.exit(f"unknown subtask op: {args.op}")

    c["updatedAt"] = now_iso()
    if touched_sid:
        c["lastTouchedSubtask"] = touched_sid
    rev = atomic_save(board, d)
    print(f"#{c['num']} subtask {action} (rev {rev})")


def cmd_link(args, d, board):
    a = find_card(d, args.a)
    b = find_card(d, args.b)
    if a["id"] == b["id"]:
        sys.exit("error: can't link a card to itself")
    a.setdefault("linkedCards", [])
    b.setdefault("linkedCards", [])
    if args.op == "link":
        added = []
        if b["id"] not in a["linkedCards"]: a["linkedCards"].append(b["id"]); added.append(f"#{a['num']}→#{b['num']}")
        if a["id"] not in b["linkedCards"]: b["linkedCards"].append(a["id"]); added.append(f"#{b['num']}→#{a['num']}")
        msg = "linked " + (", ".join(added) if added else "(already linked)")
    else:
        if b["id"] in a["linkedCards"]: a["linkedCards"].remove(b["id"])
        if a["id"] in b["linkedCards"]: b["linkedCards"].remove(a["id"])
        msg = f"unlinked #{a['num']}↔#{b['num']}"
    now = now_iso()
    a["updatedAt"] = b["updatedAt"] = now
    rev = atomic_save(board, d)
    print(f"{msg} (rev {rev})")


def cmd_column(args, d, board):
    d.setdefault("columns", [])
    if args.op == "list":
        for c in d["columns"]:
            n = sum(1 for k in d.get("cards", []) if k.get("column") == c["id"])
            print(f"  {c['id']:<14} {c.get('kind','-'):<10} {n:>3} cards  {c.get('name','')}")
        return
    if not args.id:
        sys.exit("error: column id required (e.g. `card.py column add consideration 'Consideration'`)")
    if args.op == "add":
        if any(c["id"] == args.id for c in d["columns"]):
            sys.exit(f"error: column id '{args.id}' already exists")
        col = {"id": args.id, "name": args.name or args.id.title(), "kind": args.kind}
        if args.at is None:
            d["columns"].append(col)
        else:
            d["columns"].insert(max(0, min(args.at, len(d["columns"]))), col)
        rev = atomic_save(board, d)
        print(f"+ column {args.id} (rev {rev})")
    elif args.op == "rm":
        before = len(d["columns"])
        d["columns"] = [c for c in d["columns"] if c["id"] != args.id]
        if len(d["columns"]) == before:
            sys.exit(f"error: no column '{args.id}'")
        in_use = [c for c in d.get("cards", []) if c.get("column") == args.id]
        if in_use:
            sys.exit(f"error: column '{args.id}' still has {len(in_use)} cards — move them first")
        rev = atomic_save(board, d)
        print(f"- column {args.id} (rev {rev})")
    elif args.op == "rename":
        col = next((c for c in d["columns"] if c["id"] == args.id), None)
        if not col:
            sys.exit(f"error: no column '{args.id}'")
        if not args.name:
            sys.exit("error: rename needs a new name")
        col["name"] = args.name
        rev = atomic_save(board, d)
        print(f"~ column {args.id} → '{args.name}' (rev {rev})")


def cmd_show(args, d, board):
    c = find_card(d, args.ref)
    print(json.dumps(c, indent=2, ensure_ascii=False))


def cmd_sweep_status(args, d, board):
    """#315 — report whether the inline-extraction completeness sweep was done
    for this board. A leftover extraction_pending.json = sweep skipped (the
    protocol deletes it only after the sweep). Deterministic; exit 1 if a
    sweep is still pending so scripts / the smoke harness can assert on it."""
    import sweep_status  # pure-stdlib single source of truth (hook reuses it)
    if getattr(args, "hook_line", False):
        line = sweep_status.hook_line(board)
        if line:
            print(line)
        return
    text, rc = sweep_status.human(board)
    print(text)
    if rc:
        sys.exit(rc)


def cmd_progress(args, d, board):
    """#318 — report extraction progress to the live board's BOARD-LOAD HUD.
    Best-effort: POSTs {done,total,label} to the server's /progress relay, which
    rebroadcasts it as an SSE event. No live server → silent no-op (e.g. headless).
    Never fails the caller — progress is cosmetic."""
    url = _resolve_server_url(board)
    if not url:
        return
    body = json.dumps({"done": args.done, "total": args.total,
                       "label": args.label or "",
                       "phase": getattr(args, "phase", "") or "",
                       "final": bool(getattr(args, "final", False))}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/progress", data=body, method="POST",
        headers={"Content-Type": "application/json", **_auth_headers()})
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # cosmetic; the HUD just won't advance if the relay is unreachable


def cmd_recover(args, d, board):
    """3.5c — list the rolling backups (3.5b) or restore one.

    Restoring writes the chosen backup as the new current board (rev bumped so
    SSE clients animate it). The pre-restore state is itself already a backup,
    so a restore is reversible. Dry-run by default; --apply to commit.
    """
    backups = _boardio.list_backups(board)   # [(rev, Path)] newest-first
    cur_rev = d.get("rev", 0)

    if getattr(args, "rev", None) is None:
        # LIST mode
        if not backups:
            print("no backups yet — they're written to <board>/.backups/ on every save")
            return
        print(f"{'rev':>7}  {'cards':>5}  {'savedAt':<21} savedBy")
        for rev, p in backups:
            try:
                b = json.loads(p.read_text())
                ncards, savedAt, savedBy = len(b.get("cards", [])), b.get("savedAt", "?"), b.get("savedBy", "?")
            except Exception:
                ncards, savedAt, savedBy = "?", "(unreadable)", "?"
            mark = "  ← current" if rev == cur_rev else ""
            print(f"{rev:>7}  {ncards:>5}  {savedAt:<21} {savedBy}{mark}")
        print("\nrestore with:  card.py recover <rev> --apply")
        return

    # RESTORE mode — locate + validate before touching anything.
    target = next((p for rev, p in backups if rev == args.rev), None)
    if target is None:
        avail = ", ".join(str(r) for r, _ in backups) or "none"
        sys.exit(f"error: no backup for rev {args.rev} (available: {avail})")
    try:
        restored = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"error: backup rev {args.rev} is unreadable/corrupt: {e}")
    if not isinstance(restored, dict) or not isinstance(restored.get("cards"), list):
        sys.exit(f"error: backup rev {args.rev} is not a valid board (missing cards[])")

    ncards, cur_cards = len(restored["cards"]), len(d.get("cards", []))
    if not getattr(args, "apply", False):
        print(f"DRY-RUN: would restore rev {args.rev} ({ncards} cards) over "
              f"current rev {cur_rev} ({cur_cards} cards).")
        print("Re-run with --apply to write it. Current state stays in .backups, so it's reversible.")
        return

    # #507 — a restore visibly moves cards BACK to their pre-edit columns; tag
    # each such move as an undo so the Logs HUD shows (Undo) MOVE rather than a
    # plain one. Append a history event {from: current col, to: restored col,
    # via: undo} as the LAST entry so the client's from/to guard matches THIS
    # move. The backup file on disk is untouched (we mutate the in-memory copy
    # that becomes the new live board, where the undo is now first-class history).
    cur_by_id = {c.get("id"): c for c in d.get("cards", [])}
    for rc in restored.get("cards", []):
        cur = cur_by_id.get(rc.get("id"))
        if cur and cur.get("column") != rc.get("column"):
            rc.setdefault("history", []).append({
                "from": cur.get("column"), "to": rc.get("column"),
                "at": now_iso(), "via": "undo",
            })

    # Seed rev from current so atomic_save bumps to cur_rev+1 (a forward rev
    # SSE clients accept) rather than replaying the backup's old, lower rev.
    restored["rev"] = cur_rev
    rev = atomic_save(board, restored)
    print(f"♻ restored backup rev {args.rev} → live as rev {rev} ({ncards} cards). "
          f"Pre-restore state (rev {cur_rev}) remains in .backups.")


# ===== schema migrations (3.5d) =====
# Bump SCHEMA_VERSION and append a migration whenever the card/board shape gains
# a field. Each migration MUST be idempotent (only fill what's missing) so it's
# safe to re-run and safe to apply to a board imported from an older version
# (e.g. a discover-bootstrapped board missing newer fields).
SCHEMA_VERSION = 2

# Canonical per-card fields → default factory/value (callable = fresh instance).
_CARD_DEFAULTS = {
    "code": "", "tags": list, "origin": "", "notes": "", "writeup": "",
    "linkedCards": list, "subtasks": list, "doneAt": None,
    "lastTouchedSubtask": None,
}


def _migrate_v1_card_fields(d):
    """v1 — backfill canonical per-card fields + cross-fill timestamps."""
    changed = 0
    for c in d.get("cards", []):
        for k, v in _CARD_DEFAULTS.items():
            if k not in c:
                c[k] = v() if callable(v) else v
                changed += 1
        if "updatedAt" not in c and c.get("createdAt"):
            c["updatedAt"] = c["createdAt"]; changed += 1
        if "createdAt" not in c and c.get("updatedAt"):
            c["createdAt"] = c["updatedAt"]; changed += 1
    return changed


def _migrate_v2_board_fields(d):
    """v2 — backfill board-level fields (columns, cards, nextNum)."""
    changed = 0
    if "columns" not in d: d["columns"] = []; changed += 1
    if "cards" not in d: d["cards"] = []; changed += 1
    if "nextNum" not in d:
        d["nextNum"] = max((c.get("num", 0) for c in d.get("cards", [])), default=0) + 1
        changed += 1
    return changed


MIGRATIONS = [
    (1, "backfill canonical per-card fields", _migrate_v1_card_fields),
    (2, "backfill board-level fields (columns, cards, nextNum)", _migrate_v2_board_fields),
]


def cmd_migrate(args, d, board):
    """3.5d — apply idempotent schemaVersion migrations. Dry-run by default."""
    cur = d.get("schemaVersion", 0)
    pending = [(v, name, fn) for v, name, fn in MIGRATIONS if v > cur]
    if not pending:
        print(f"schema up to date (schemaVersion {cur}, latest {SCHEMA_VERSION}) — nothing to migrate")
        return

    if not getattr(args, "apply", False):
        print(f"DRY-RUN: schemaVersion {cur} → {SCHEMA_VERSION}, {len(pending)} migration(s) pending:")
        probe = copy.deepcopy(d)
        for v, name, fn in pending:
            n = fn(probe)  # mutates the throwaway copy only
            print(f"  v{v}  {name}  ({n} field(s) would change)")
        print("Re-run with --apply to write. Current state stays in .backups, so it's reversible.")
        return

    total = 0
    for v, name, fn in pending:
        n = fn(d)
        total += n
        print(f"  v{v}  {name}  ({n} field(s) changed)")
    d["schemaVersion"] = SCHEMA_VERSION
    rev = atomic_save(board, d)
    print(f"✓ migrated to schemaVersion {SCHEMA_VERSION} ({total} field(s) backfilled) (rev {rev})")


def cmd_repair_links(args, d, board):
    """3.5e — walk linkedCards and fix integrity: drop dangling (target gone),
    self, and duplicate ids; restore reciprocity for one-sided links (links are
    bidirectional by design). Dry-run by default; idempotent on re-run."""
    cards = d.get("cards", [])
    by_id = {c["id"]: c for c in cards if "id" in c}

    dangling = []   # (num, bad_id)
    selflinks = []  # (num,)
    dupes = []      # (num, dup_id)
    onesided = []   # (num, other_id) — other exists but doesn't link back
    for c in cards:
        seen = set()
        for oid in (c.get("linkedCards") or []):
            if oid == c.get("id"):
                selflinks.append((c.get("num"),)); continue
            if oid in seen:
                dupes.append((c.get("num"), oid)); continue
            seen.add(oid)
            if oid not in by_id:
                dangling.append((c.get("num"), oid)); continue
            if c.get("id") not in (by_id[oid].get("linkedCards") or []):
                onesided.append((c.get("num"), oid))

    total = len(dangling) + len(selflinks) + len(dupes) + len(onesided)
    if total == 0:
        print("links healthy — nothing to repair")
        return

    def _num(oid):
        return f"#{by_id[oid]['num']}" if oid in by_id else f"{oid}(gone)"

    print(f"{total} link issue(s) found:")
    for num, oid in dangling:  print(f"  drop dangling   #{num} → {oid} (no such card)")
    for (num,) in selflinks:   print(f"  drop self-link  #{num} → itself")
    for num, oid in dupes:     print(f"  drop duplicate  #{num} → {_num(oid)}")
    for num, oid in onesided:  print(f"  add reciprocal  {_num(oid)} → #{num} (currently one-sided)")

    if not getattr(args, "apply", False):
        print("\nRe-run with --apply to fix. Current state stays in .backups, so it's reversible.")
        return

    # 1) Clean each list: drop self/dangling/dupes, preserve order.
    for c in cards:
        seen, cleaned = set(), []
        for oid in (c.get("linkedCards") or []):
            if oid == c.get("id") or oid not in by_id or oid in seen:
                continue
            seen.add(oid); cleaned.append(oid)
        c["linkedCards"] = cleaned
    # 2) Restore reciprocity for every surviving link.
    recip = 0
    for c in cards:
        for oid in c["linkedCards"]:
            ol = by_id[oid].setdefault("linkedCards", [])
            if c["id"] not in ol:
                ol.append(c["id"]); recip += 1
    rev = atomic_save(board, d)
    print(f"✓ repaired: {len(dangling)} dangling, {len(selflinks)} self, "
          f"{len(dupes)} dupe dropped; {recip} reciprocal(s) added (rev {rev})")


# ===== prelaunch gate (#91) =====
# Cards in launch-blocking columns/priorities that aren't shipped or blocked.
# Surface these before any public-facing ship: github-repo flip private→public,
# `gh release create`, `npm publish`, marketing send, DNS go-live. Claude calls
# this before launch-shaped actions; SessionStart hook also injects a count.

_LAUNCH_BLOCKING_COLS = ("super-urgent",)   # #104 — mandatory retired; super-urgent is the one urgent column
_LAUNCH_BLOCKING_PRIOS = ("critical", "mid")


def _prelaunch_open_cards(d: dict) -> list[dict]:
    """Return list of cards that block launch.

    Rule (from card #91): in super-urgent or mandatory column, priority
    critical or mid, not in done/blocked. Sorted: super-urgent first, then
    by priority (critical → mid), then by card num."""
    open_cards = []
    for c in d.get("cards", []):
        col = c.get("column")
        if col not in _LAUNCH_BLOCKING_COLS:
            continue
        if (c.get("priority") or "low") not in _LAUNCH_BLOCKING_PRIOS:
            continue
        open_cards.append(c)
    open_cards.sort(key=lambda c: (
        0 if c.get("column") == "super-urgent" else 1,
        0 if c.get("priority") == "critical" else 1,
        c.get("num", 0),
    ))
    return open_cards


def cmd_prelaunch_check(args, d, board):
    open_cards = _prelaunch_open_cards(d)
    if args.json:
        out = [{
            "num": c["num"],
            "code": c.get("code") or c.get("id"),
            "column": c.get("column"),
            "priority": c.get("priority"),
            "title": c.get("title", ""),
        } for c in open_cards]
        print(json.dumps({"open_count": len(open_cards), "items": out}, indent=2))
    elif args.count:
        print(len(open_cards))
    else:
        if not open_cards:
            print("✅ prelaunch-check: 0 blocking items — clear to launch")
        else:
            print(f"⚠️  prelaunch-check: {len(open_cards)} item(s) still open")
            for c in open_cards:
                col = "SUPER URGENT" if c["column"] == "super-urgent" else "MANDATORY  "
                p = (c.get("priority") or "-")[:1].upper()
                code = c.get("code") or c.get("id")
                print(f"  [{col}] #{c['num']:>3} [{p}] {code:<18} {c.get('title','')[:60]}")
            print()
            print(f"Run `card.py show <num>` for detail, or `card.py fly <num> done` to ship.")
    sys.exit(0 if not open_cards else 9)


def cmd_list(args, d, board):
    cards = d["cards"]
    if args.column:
        cards = [c for c in cards if c.get("column") == args.column]
    if args.priority:
        cards = [c for c in cards if c.get("priority") == args.priority]
    if args.tag:
        cards = [c for c in cards if args.tag in (c.get("tags") or [])]
    for c in cards:
        p = (c.get("priority") or "-")[:1].upper()
        code = c.get("code") or c.get("id")
        print(f"  #{c['num']:>3} [{p}] {c.get('column'):<10} {code:<14} {c.get('title','')[:70]}")
    print(f"({len(cards)} cards)")


# ===== Phase 5: token-efficiency read tier (query / digest / wiki) =====
#
# The progressive-disclosure ladder (VISION pillar #2):
#   digest  → ~120-tok board pulse (counts + last-shipped + launch-blocking)
#   query   → sliced JSON, only the fields you ask for, machine-readable
#   show    → one full card
#   board.json → the whole thing (last resort)
# `list` stays the human-readable text view; `query` is its JSON sibling so an
# agent pulls exactly the columns it needs without paying for notes/writeups.

_DIGEST_ORDER = ["super-urgent", "ideas", "task",
                 "backlog", "inprogress", "blocked", "done"]


def _ago(iso: str | None) -> str:
    """Relative time like '<1h ago' / '5h ago' / '3d ago'. '' on bad input."""
    if not iso:
        return ""
    try:
        when = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = datetime.datetime.now(datetime.timezone.utc) - when
        hrs = int(delta.total_seconds() // 3600)
        if hrs < 1:
            return "<1h ago"
        if hrs < 24:
            return f"{hrs}h ago"
        return f"{hrs // 24}d ago"
    except Exception:
        return iso[:10]


def cmd_digest(args, d, board):
    """5a — the compact board pulse, on demand. Same shape the SessionStart
    hook injects, but callable mid-session so Claude refreshes without
    re-reading board.json. ~120 tokens of text (or --json)."""
    cards = d.get("cards", [])
    cols = {c["id"]: c.get("name", c["id"]) for c in d.get("columns", [])}
    counts: dict[str, int] = {}
    for c in cards:
        counts[c.get("column", "?")] = counts.get(c.get("column", "?"), 0) + 1

    done = sorted((c for c in cards if c.get("column") == "done"),
                  key=lambda c: c.get("doneAt") or "", reverse=True)
    last = ""
    if done:
        t = done[0]
        last = f"#{t.get('num','?')} {t.get('code') or t.get('id','')} ({_ago(t.get('doneAt'))})"

    blocking = sum(
        1 for c in cards
        if c.get("column") in _LAUNCH_BLOCKING_COLS
        and (c.get("priority") or "low") in _LAUNCH_BLOCKING_PRIOS
    )

    if getattr(args, "json", False):
        ordered = {k: counts[k] for k in _DIGEST_ORDER if counts.get(k)}
        for k, n in counts.items():
            if k not in ordered and n:
                ordered[k] = n
        print(json.dumps({
            "rev": d.get("rev", 0),
            "totalCards": len(cards),
            "counts": ordered,
            "lastShipped": last,
            "launchBlocking": blocking,
        }, ensure_ascii=False))
        return

    parts, seen = [], set()
    for k in _DIGEST_ORDER:
        if counts.get(k):
            parts.append(f"{cols.get(k, k)}: {counts[k]}")
            seen.add(k)
    for k, n in counts.items():
        if k not in seen and n:
            parts.append(f"{cols.get(k, k)}: {n}")
    print(f"rev {d.get('rev', 0)} · {len(cards)} cards · " + " · ".join(parts))
    if last:
        print(f"Last shipped: {last}")
    if blocking:
        print(f"🚨 LAUNCH-BLOCKING: {blocking} open · run `card.py prelaunch-check` before any launch/publish action")


# Convenience aliases so callers can use index.json short keys or card keys.
_QUERY_FIELD_ALIASES = {
    "n": "num", "col": "column", "prio": "priority",
    "upd": "updatedAt", "done": "doneAt", "created": "createdAt",
}
_QUERY_DEFAULT_FIELDS = ["num", "code", "title", "column", "priority", "updatedAt"]


def cmd_query(args, d, board):
    """5a — sliced JSON view. Same filters as `list`, but emits a JSON array
    with only the fields requested (default: a compact 6-field projection).
    The token-efficient machine tier between `digest` and `show`.

    --fields p          → subtask progress 'done/total'
    --fields links      → count of linkedCards
    --fields all        → whole cards (= multi-card `show`)
    """
    cards = list(d.get("cards", []))
    if args.column:
        cards = [c for c in cards if c.get("column") == args.column]
    if args.priority:
        cards = [c for c in cards if c.get("priority") == args.priority]
    if args.tag:
        cards = [c for c in cards if args.tag in (c.get("tags") or [])]
    if args.since_days is not None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=args.since_days))
        kept = []
        for c in cards:
            try:
                upd = datetime.datetime.fromisoformat(
                    (c.get("updatedAt") or "").replace("Z", "+00:00"))
                if upd >= cutoff:
                    kept.append(c)
            except Exception:
                pass
        cards = kept

    # Sort newest-updated first so the most relevant rows lead.
    cards.sort(key=lambda c: c.get("updatedAt") or "", reverse=True)
    if args.limit is not None:
        cards = cards[:args.limit]

    raw_fields = [f.strip() for f in (args.fields or "").split(",") if f.strip()]
    if raw_fields == ["all"]:
        print(json.dumps(cards, indent=2, ensure_ascii=False))
        return
    fields = [_QUERY_FIELD_ALIASES.get(f, f) for f in raw_fields] or _QUERY_DEFAULT_FIELDS

    def project(c: dict) -> dict:
        out = {}
        for f in fields:
            if f == "p":
                subs = c.get("subtasks") or []
                done_n = sum(1 for s in subs if s.get("done"))
                out["p"] = f"{done_n}/{len(subs)}" if subs else ""
            elif f == "links":
                out["links"] = len(c.get("linkedCards") or [])
            else:
                out[f] = c.get(f)
        return out

    print(json.dumps([project(c) for c in cards], ensure_ascii=False))


def cmd_wiki(args, d, board):
    """5c (nice-to-have) — pre-rendered narrative Markdown of the board, for a
    human glance or a paste into a PR/standup. Shares the renderer with
    `export` and serve.py /export.md (see _render.py)."""
    print(_render.to_markdown(d, recent=args.recent))


def cmd_metrics(args, d, board):
    """5.5b (#114) — velocity metrics: throughput, cycle time, blockers,
    priority drift. Text summary by default; --json for the raw dict (same
    payload serve.py /metrics returns)."""
    m = _metrics.compute(d, since_days=args.since_days)
    if getattr(args, "json", False):
        print(json.dumps(m, ensure_ascii=False, indent=2))
    else:
        print(_metrics.to_text(m))


def cmd_export(args, d, board):
    """5.5c (#115) — write a shareable snapshot (Markdown or HTML) to a file or
    stdout. Format inferred from --to extension (.html → HTML, else Markdown),
    or forced with --format. --since-days N narrows the 'Recently shipped'
    section to a sprint window. For CI: `card.py export --since-days 7 --to sprint.html`."""
    fmt = args.format
    if fmt is None:
        fmt = "html" if (args.to or "").lower().endswith((".html", ".htm")) else "md"
    render = _render.to_html if fmt == "html" else _render.to_markdown
    body = render(d, recent=args.recent, since_days=args.since_days)
    if args.to:
        Path(args.to).write_text(body, encoding="utf-8")
        print(f"exported {fmt} snapshot → {args.to} ({len(body)} bytes, rev {d.get('rev', 0)})")
    else:
        print(body)
