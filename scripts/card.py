#!/usr/bin/env python3
"""board card CLI — mutate board.json without writing dict literals every time.

Handles the boilerplate (load, mutate, bump rev, set savedAt/savedBy='claude',
atomic write, regen index.json) so callers just say what they want changed.

All long-text fields (origin, notes, writeup) accept either a literal flag
value OR `--<field>-stdin` to read from stdin (avoids shell-quoting pain).

Usage examples (run from any cwd; --board defaults to ./board/board.json
walking up the tree):

    # Add a new card to the Ideas column
    card.py add --code BOARD-EVOLVE --column ideas --priority low \\
      --title "Let users self-evolve the skill" \\
      --origin "User asked: ..." --link c-board-v3

    # Fly card to Done with a writeup
    card.py fly 66 done --writeup-stdin <<< "Shipped foo. Verified bar."

    # Update fields
    card.py update 14 --priority critical --add-tag urgent

    # Subtask ops
    card.py subtask add 65 "Eyeball in Safari + Firefox"
    card.py subtask done 65 s-v3-5
    card.py subtask rm 65 s-v3-5

    # Bidirectional link (also unlink)
    card.py link 66 65
    card.py unlink 66 65

    # Quick read (compact)
    card.py show 65
    card.py list --column inprogress

THE 5 CANONICAL LIFECYCLE TRANSITIONS (see VISION.md §4):
    1. CREATE              card.py add --title "..." --column task --priority mid
    2. BEGIN               card.py fly <ref> inprogress
    3. SHIP                card.py fly <ref> done --writeup "..."
    4. REOPEN-AS-BUG       card.py bug <ref>                (Done → IP + 'bug' tag)
    5. REOPEN-AS-IMPROVE   card.py improve <ref> "..."     (Done → IP + new subtask)

Plus the end-to-end wrappers:
    card.py sim                 — task → ip → done (canonical happy path)
    card.py sim --with-bug      — task → ip → done → reopen → ip → done
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import _boardio                  # noqa: E402  (board_lock for the no-server path)
import card_state                # noqa: E402  (set _HOLDING_LOCK on the module)
from card_state import *         # noqa: E402,F401,F403  (find_board/load/atomic_save/...)
from card_commands import *      # noqa: E402,F401,F403  (cmd_* referenced by build_parser)


# ===== argparse wiring =====

def build_parser():
    ap = argparse.ArgumentParser(prog="card", description="board card CLI")
    ap.add_argument("--board", type=Path, default=None,
                    help="path to board.json (default: ./board/board.json, walks up)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # add
    pa = sub.add_parser("add", help="add a new card")
    pa.add_argument("--title", required=True)
    pa.add_argument("--column", default="backlog",
                    )
    pa.add_argument("--priority", choices=["critical","mid","low"], default=None)
    pa.add_argument("--code", default=None, help="optional short badge like 'FACT9'")
    pa.add_argument("--id", default=None, help="explicit id (default: slug of code/title)")
    pa.add_argument("--tag", action="append", default=None, help="repeat for multiple tags")
    pa.add_argument("--origin", default=None)
    pa.add_argument("--origin-stdin", action="store_true")
    pa.add_argument("--notes", default=None)
    pa.add_argument("--notes-stdin", action="store_true")
    pa.add_argument("--writeup", default=None)
    pa.add_argument("--writeup-stdin", action="store_true")
    pa.add_argument("--link", action="append", default=None, help="ref(s) to link bidirectionally")
    pa.add_argument("--created-at", default=None,
                    help="ISO timestamp to override createdAt (default: now). "
                         "Use when importing historic work so board sorts chronologically.")
    pa.add_argument("--force", action="store_true",
                    help="Accept tags that aren't in board.json tagTaxonomy (otherwise blocked w/ close-match suggestion)")
    pa.add_argument("--urgent", action="store_true",
                    help="Force-route this card to 🚨 SUPER URGENT col with critical priority "
                         "(creates the column if missing).")
    pa.add_argument("--no-auto-urgent", action="store_true",
                    help="Skip urgency-keyword detection in title/origin (#85). Use when "
                         "the words 'urgent/asap/blocker/...' are part of the card content, "
                         "not a real urgency signal.")
    pa.add_argument("--auto", action="store_true",
                    help="Mark this card as auto-created from intent detection (#100). "
                         "Defaults --column to 'ideas' (created if missing) and stamps "
                         "meta.autoCreated so the board pops a 5s Undo toast.")
    pa.add_argument("--auto-source", default=None,
                    help="The user phrase that triggered auto-card (e.g. 'I have an idea:'). "
                         "Stored in meta.autoSource + shown in the Undo toast.")
    pa.set_defaults(fn=cmd_add)

    # update
    pu = sub.add_parser("update", help="patch fields on an existing card")
    pu.add_argument("ref", help="#N or id or code")
    pu.add_argument("--title", default=None)
    pu.add_argument("--code", default=None)
    pu.add_argument("--column", default=None,
                    )
    pu.add_argument("--priority", default=None, choices=["critical","mid","low"])
    pu.add_argument("--origin", default=None)
    pu.add_argument("--origin-stdin", action="store_true")
    pu.add_argument("--notes", default=None)
    pu.add_argument("--notes-stdin", action="store_true")
    pu.add_argument("--writeup", default=None)
    pu.add_argument("--writeup-stdin", action="store_true")
    pu.add_argument("--add-tag", action="append", default=None)
    pu.add_argument("--rm-tag", action="append", default=None)
    pu.add_argument("--add-linked-file", action="append", default=None,
                    help="path to a file this card 'owns' (#102 AUTO-LINK). When the "
                         "PreToolUse hook sees Edit/Write on this path, the board flashes "
                         "this card's border. Paths are normalised to absolute form.")
    pu.add_argument("--rm-linked-file", action="append", default=None,
                    help="remove a linked-file path (matched after the same abs-path normalisation)")
    pu.add_argument("--force", action="store_true",
                    help="Accept tags that aren't in board.json tagTaxonomy")
    pu.set_defaults(fn=cmd_update)

    # fly — THE column-change verb (move was removed: it jumped, fly animates)
    pfy = sub.add_parser("fly", help="change a card's column (animated); --bug/--improve/--note shortcuts + animation pause")
    pfy.add_argument("ref")
    pfy.add_argument("column", help="destination column id")
    pfy.add_argument("--bug", metavar="REASON", default=None,
                     help="add 'bug' tag + 🐞 fix-bug subtask")
    pfy.add_argument("--improve", metavar="TEXT", default=None,
                     help="add improvement subtask")
    pfy.add_argument("--subtask", metavar="TEXT", default=None,
                     help="add plain subtask")
    pfy.add_argument("--note", metavar="TEXT", default=None,
                     help="append to notes")
    pfy.add_argument("--writeup", default=None, help="set writeup (typical for done)")
    pfy.add_argument("--writeup-stdin", action="store_true",
                     help="read writeup from stdin (e.g. heredoc)")
    pfy.add_argument("--pause-ms", type=int, default=400,
                     help="sleep N ms after save (default 400, matches simulateUserDragMove)")
    pfy.add_argument("--force", action="store_true",
                     help="override the decompose-before-IP guard (genuinely one atomic task)")
    pfy.set_defaults(fn=cmd_fly)

    # subtask
    ps = sub.add_parser("subtask", help="subtask ops")
    ps.add_argument("op", choices=["add","done","undone","rm"])
    ps.add_argument("ref", help="card ref (#N / id / code)")
    ps.add_argument("text_or_sid", help="text (for add) or subtask id (for done/undone/rm)")
    ps.add_argument("--parent", default=None, help="parent subtask id (for nested add)")
    def _subtask_dispatch(args, d, board):
        if args.op == "add":
            args.text = args.text_or_sid; args.sid = None
        else:
            args.sid = args.text_or_sid; args.text = None
        cmd_subtask(args, d, board)
    ps.set_defaults(fn=_subtask_dispatch)

    # link / unlink
    pl = sub.add_parser("link", help="bidirectionally link two cards")
    pl.add_argument("a"); pl.add_argument("b")
    pl.set_defaults(fn=lambda args, d, board: cmd_link(argparse.Namespace(**vars(args), op="link"), d, board))

    pul = sub.add_parser("unlink", help="remove a bidirectional link")
    pul.add_argument("a"); pul.add_argument("b")
    pul.set_defaults(fn=lambda args, d, board: cmd_link(argparse.Namespace(**vars(args), op="unlink"), d, board))

    # column ops — add/rm/rename. Custom columns emit column-added SSE events
    # so the UI slides them in alongside any subsequent cards.
    pc = sub.add_parser("column", help="column add/rm/rename")
    pc.add_argument("op", choices=["add","rm","rename","list"])
    pc.add_argument("id", nargs="?", help="column id (e.g. 'consideration')")
    pc.add_argument("name", nargs="?", help="display name (for add/rename)")
    pc.add_argument("--kind", default="custom",
                    help="column kind hint (intake|todo|active|blocked|done|custom)")
    pc.add_argument("--at", type=int, default=None,
                    help="position to insert at (0-based); default = append")
    pc.set_defaults(fn=cmd_column)

    # show / list
    psh = sub.add_parser("show", help="print one card as JSON")
    psh.add_argument("ref")
    psh.set_defaults(fn=cmd_show)

    # sweep-status (#315) — the forgotten-sweep guard. Exit 1 if a leftover
    # extraction_pending.json means the completeness sweep was never run.
    pss = sub.add_parser("sweep-status",
                         help="report whether the inline-extraction completeness "
                              "sweep is still pending (leftover extraction_pending.json). "
                              "Exit 1 if pending. The session-start hook reuses this.")
    pss.add_argument("--hook-line", action="store_true", dest="hook_line",
                     help="emit the one-line session-start klaxon (empty if clean); "
                          "never exits nonzero")
    pss.set_defaults(fn=cmd_sweep_status)

    # progress (#318) — report extraction progress to the live BOARD-LOAD HUD.
    ppr = sub.add_parser("progress",
                         help="report inline/haiku extraction progress to the live "
                              "board HUD (best-effort SSE relay; no-op if no server).")
    ppr.add_argument("--done", type=int, required=True, help="chunks completed so far")
    ppr.add_argument("--total", type=int, required=True, help="total chunks staged")
    ppr.add_argument("--label", default=None, help="current chunk label (e.g. its time window)")
    ppr.add_argument("--phase", default="", help="fill stage for the HUD header "
                     "(inline / replay / speedup / solo / reconcile); '' = inline default")
    ppr.add_argument("--final", action="store_true",
                     help="this is the LAST emit of the whole fill — the HUD "
                          "completes (✓) and auto-hides; otherwise it stays visible "
                          "and hands off to the next stage (#327 single-HUD).")
    ppr.set_defaults(fn=cmd_progress)

    # recover (3.5c) — list rolling backups or restore one
    prc = sub.add_parser("recover", help="list rolling backups, or restore one (3.5c)")
    prc.add_argument("rev", nargs="?", type=int, help="backup rev to restore (omit to list)")
    prc.add_argument("--apply", action="store_true",
                     help="actually write the restore (default: dry-run)")
    prc.set_defaults(fn=cmd_recover)

    # migrate (3.5d) — apply idempotent schemaVersion migrations
    pmg = sub.add_parser("migrate", help="apply schemaVersion migrations (3.5d)")
    pmg.add_argument("--apply", action="store_true",
                     help="actually run the migrations (default: dry-run)")
    pmg.set_defaults(fn=cmd_migrate)

    # repair-links (3.5e) — fix dangling/self/dupe/one-sided linkedCards
    prl = sub.add_parser("repair-links", help="fix linkedCards integrity (3.5e)")
    prl.add_argument("--apply", action="store_true",
                     help="actually apply the fixes (default: dry-run)")
    prl.set_defaults(fn=cmd_repair_links)

    pbug = sub.add_parser("bug", help="reopen a Done card as a bug (Done → In Progress + 'bug' tag + 🐞 fix-bug subtask)")
    pbug.add_argument("ref", help="card num or id")
    pbug.add_argument("--reason", help="optional reason — becomes the bug-fix subtask text")
    pbug.set_defaults(fn=cmd_bug)

    pimp = sub.add_parser("improve", help="add an improvement subtask + reopen (Done → In Progress + new subtask)")
    pimp.add_argument("ref", help="card num or id")
    pimp.add_argument("text", help="subtask text (the improvement)")
    pimp.set_defaults(fn=cmd_improve)

    pas = sub.add_parser("auto-ship",
                         help="auto-promote inprogress cards to done using git log (#101). "
                              "No ref = scan mode (table of candidates); ref + --apply = ship that one.")
    pas.add_argument("ref", nargs="?", default=None,
                     help="card num/code/id to ship (omit for scan mode)")
    pas.add_argument("--since-ref", default="HEAD~1",
                     help="git ref starting bound; commits in <ref>..HEAD are scanned (default HEAD~1)")
    pas.add_argument("--writeup-extra", default=None,
                     help="append this prose to the auto-generated writeup")
    pas.add_argument("--apply", action="store_true",
                     help="actually move the card (default is dry-run preview)")
    pas.add_argument("--force", action="store_true",
                     help="re-ship a card already in done")
    pas.set_defaults(fn=cmd_auto_ship)

    psim = sub.add_parser("sim", help="run canonical lifecycle: task → inprogress → done")
    psim.add_argument("--title", default=None, help="card title (default: auto-named)")
    psim.add_argument("--priority", default="mid", choices=["critical", "mid", "low"])
    psim.add_argument("--intervals", default="2,5",
                      help="seconds between phases: 'task→ip,ip→done' (default '2,5')")
    psim.add_argument("--writeup", default=None, help="custom done writeup (auto if omitted)")
    psim.add_argument("--with-bug", action="store_true",
                      help="after done, reopen as bug (+'bugged' tag), then re-finish")
    psim.set_defaults(fn=cmd_sim)

    pls = sub.add_parser("list", help="list cards (filtered)")
    pls.add_argument("--column", default=None)
    pls.add_argument("--priority", default=None)
    pls.add_argument("--tag", default=None)
    pls.set_defaults(fn=cmd_list)

    # digest (5a) — compact board pulse on demand (same shape as SessionStart hook)
    pdg = sub.add_parser("digest",
                         help="print the compact board pulse (counts + last-shipped + "
                              "launch-blocking). ~120 tokens; --json for machine form.")
    pdg.add_argument("--json", action="store_true", help="emit JSON instead of text")
    pdg.set_defaults(fn=cmd_digest)

    # query (5a) — sliced JSON, the machine sibling of `list`
    pq = sub.add_parser("query",
                        help="sliced JSON view: same filters as list, only the --fields "
                             "you ask for (default 6-field projection). Token-efficient.")
    pq.add_argument("--column", default=None)
    pq.add_argument("--priority", default=None)
    pq.add_argument("--tag", default=None)
    pq.add_argument("--since-days", type=int, default=None, dest="since_days",
                    help="only cards updated within the last N days")
    pq.add_argument("--limit", type=int, default=None, help="cap the number of rows")
    pq.add_argument("--fields", default=None,
                    help="comma-list of fields (aliases: n,col,prio,upd,done,created; "
                         "specials: p=subtask progress, links=link count, all=full cards). "
                         "Default: num,code,title,column,priority,updatedAt")
    pq.set_defaults(fn=cmd_query)

    # wiki (5c) — narrative Markdown render of the board
    pwk = sub.add_parser("wiki",
                         help="pre-rendered narrative Markdown of the board (nice-to-have, "
                              "for a human glance / PR paste).")
    pwk.add_argument("--recent", type=int, default=10,
                     help="how many recently-shipped cards to lead with (default 10)")
    pwk.set_defaults(fn=cmd_wiki)

    # metrics (5.5b · #114) — velocity dashboard data
    pmt = sub.add_parser("metrics",
                         help="velocity metrics: throughput, cycle time, blockers, "
                              "priority drift. --json for the raw dict.")
    pmt.add_argument("--since-days", type=int, default=7, dest="since_days",
                     help="window in days (default 7)")
    pmt.add_argument("--json", action="store_true", help="emit the raw metrics dict")
    pmt.set_defaults(fn=cmd_metrics)

    # export (5.5c · #115) — write a shareable HTML/Markdown snapshot
    pex = sub.add_parser("export",
                         help="write a shareable snapshot (Markdown or HTML) to a file or "
                              "stdout — for showing a sprint to your boss. CI-friendly.")
    pex.add_argument("--to", default=None,
                     help="output file (extension picks format); omit to print to stdout")
    pex.add_argument("--format", choices=["md", "html"], default=None,
                     help="force format (default: infer from --to, else md)")
    pex.add_argument("--since-days", type=int, default=None, dest="since_days",
                     help="narrow 'Recently shipped' to the last N days (sprint window)")
    pex.add_argument("--recent", type=int, default=20,
                     help="max recently-shipped cards to lead with (default 20)")
    pex.set_defaults(fn=cmd_export)

    # prelaunch-check (#91) — exit 9 if any super-urgent items open
    ppl = sub.add_parser("prelaunch-check",
                         help="exit 9 if any super-urgent cards still open. "
                              "Run BEFORE any public-facing ship (gh release, npm publish, "
                              "DNS go-live, repo flip private→public).")
    ppl.add_argument("--json", action="store_true", help="emit JSON instead of human text")
    ppl.add_argument("--count", action="store_true", help="emit just the open count (for shell scripts)")
    ppl.set_defaults(fn=cmd_prelaunch_check)

    return ap



# Classification verbs we count (#382). The open question: does surfacing
# bug/improve in the UserPromptSubmit hook actually shift work off the generic
# `add` path onto the correct classifier? We log every use of these so report.py
# can show the add-vs-bug-vs-improve mix over time and answer it with data.
_COUNTED_VERBS = ("add", "bug", "improve")

# Commands that only READ the board — they must not move the "last-active board"
# pointer (a background `card.py show --board X` shouldn't make X the board the
# SessionStart hook reopens at $HOME). Everything else is a mutation and signals
# "the human is working on this board".
_READ_ONLY_CMDS = frozenset({
    "show", "list", "digest", "query", "metrics", "export", "wiki",
    "progress", "sweep-status", "prelaunch-check",
})
# Note: `recover` is NOT read-only — `recover --apply` restores a backup
# (atomic_save), the strongest "I'm working on this board" signal there is, so
# it must update last-active. The list-only `recover` (no --apply) harmlessly
# marks active too, which is fine.


def _mark_active(args, board) -> None:
    """Record the mutated board as last-active so SessionStart reopens it at
    $HOME (replaces the mtime tie-break). Best-effort — never breaks a write."""
    if getattr(args, "cmd", None) in _READ_ONLY_CMDS:
        return
    try:
        import port_registry
        port_registry.set_active(Path(board).parent)
    except Exception:
        pass


def _log_verb_usage(args, board) -> None:
    """Best-effort: record which classification verb was used, via the shared
    telemetry writer (NOT a parallel counter file). Silent on any failure — a
    telemetry hiccup must never break a card write."""
    cmd = getattr(args, "cmd", None)
    if cmd not in _COUNTED_VERBS:
        return
    try:
        import log_event
        ev = {"trigger": "card-verb", "verb": cmd,
              "project": str(Path(board).parent.resolve())}
        if cmd == "add":
            # Distinguish a net-new defect filed as `add --tag bug` from a plain
            # task — that's the alternative to the reopen-only `bug` verb.
            tags = getattr(args, "tag", None) or []
            ev["column"] = getattr(args, "column", None)
            ev["tagged_bug"] = "bug" in tags
        log_event.write_event(ev)
    except Exception:
        pass


def main():
    args = build_parser().parse_args()
    board = find_board(args.board)

    # If a server owns this board, writes funnel through it (POST) and the
    # server serializes them — no file lock here (holding it during a POST would
    # deadlock the server's own locked write). If NOT, we write directly, so we
    # hold the file lock across load→dispatch→save: the read is then fresh under
    # the lock and concurrent direct writers can't lose each other's updates.
    server_present = (
        os.environ.get("BOARD_NO_SERVER") != "1"
        and _resolve_server_url(board) is not None
    )
    if server_present:
        d = load(board)
        args.fn(args, d, board)
    else:
        with _boardio.board_lock(board):
            card_state._HOLDING_LOCK = True
            try:
                d = load(board)
                args.fn(args, d, board)
            finally:
                card_state._HOLDING_LOCK = False

    # Count the classification verb AFTER the write committed (so failed ops
    # aren't counted). Best-effort — never raises. (#382)
    _log_verb_usage(args, board)
    # Record this board as last-active (mutations only) so the SessionStart hook
    # reopens the board the human is actually working on. Best-effort. (#mb)
    _mark_active(args, board)


if __name__ == "__main__":
    main()
