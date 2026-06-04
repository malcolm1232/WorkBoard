#!/usr/bin/env bash
# board-steward SessionStart hook.
#
# Fires ONCE per Claude Code session, regardless of CWD or first-prompt phase.
# Injects a tight digest (~80 tokens) into context so Claude knows the board
# exists, its current shape, and the update protocol — without loading the
# full board.json on every prompt.
#
# Output goes straight into Claude's context. Exit 0 always (non-blocking).
# Cost: <100ms (one curl + one python parse of board.json).

set +e
set -u

# Walk up from CWD looking for board/board.json (max 8 levels).
dir="${PWD}"
board_path=""
for _ in 1 2 3 4 5 6 7 8; do
  if [ -f "${dir}/board/board.json" ]; then
    board_path="${dir}/board/board.json"
    break
  fi
  parent="$(dirname "${dir}")"
  [ "${parent}" = "${dir}" ] && break
  dir="${parent}"
done

# Fallback: if CWD-walk failed, search recent Claude session jsonls for a
# board project DESCENDED FROM the current PWD. Catches "claude opened in
# $HOME but my project is under it" without surfacing unrelated boards.
if [ -z "${board_path}" ]; then
  finder="$(dirname "$0")/_hook_find_board.py"
  if [ -f "${finder}" ]; then
    candidate="$(python3 "${finder}" "${PWD}" 2>/dev/null)"
    if [ -n "${candidate}" ] && [ -f "${candidate}" ]; then
      board_path="${candidate}"
    fi
  fi
fi

# ── First-run auto-bootstrap (#onboarding) ────────────────────────────────────
# No board found by walk-up. On a FRESH plugin install (no global onboarded
# marker yet) we bootstrap ONE board in the current project, so the very first
# session after `claude plugin install` opens a live, self-filling board instead
# of the silent "huh, now what?" dead-end. The /plugin path only wires hooks —
# it never ran install.sh's bootstrap+autostart+open — so we do it here.
# Fires AT MOST ONCE (the marker) and NEVER in $HOME / "/" (too broad — we'd
# litter a board in a non-project dir). After that first board, additional boards
# are explicit (serve.py --bootstrap, or just ask Claude). Opt out with
# BOARD_NO_AUTO_BOOTSTRAP=1 (CI/headless/demo).
onboard_marker="${HOME}/.board-steward/.onboarded"
if [ -z "${board_path}" ]; then
  # Opted out → preserve the original silent exit (CI/headless/demo).
  if [ "${BOARD_NO_AUTO_BOOTSTRAP:-0}" = "1" ]; then
    exit 0
  fi

  # Already onboarded but the CWD-walk + finder found no board (the usual
  # "launched claude in $HOME" case). DON'T silently exit — that's the bug that
  # let Claude freelance a generic greeting instead of opening the board. The
  # board IS the home screen: resolve the user's REGISTERED board from the sticky
  # port-assignments map and fall through to the shared block below, which probes
  # its server, auto-opens the browser iff sseClients==0 (compulsory open at
  # SessionStart = at launch, without re-popping a tab already viewing), and
  # injects the digest. Multi-board disambiguation: reopen the LAST-ACTIVE board
  # (the one whose cards the human last mutated / the board they just bootstrapped
  # — port_registry.get_active()), NOT whichever board.json has the newest file
  # mtime. mtime picked the wrong board when two boards were touched the same
  # session (#mb). Fall back to most-recently-updated board.json only when no
  # active pointer exists yet (e.g. first session after an upgrade).
  if [ -f "${onboard_marker}" ]; then
    hook_dir="$(dirname "$0")"
    board_path="$(python3 -c "
import sys; sys.path.insert(0, sys.argv[1])
from pathlib import Path
import port_registry as pr
active = pr.get_active()
if active:
    bj = Path(active) / 'board.json'
    if bj.exists():
        print(str(bj)); raise SystemExit
best = None
for d in pr.assignments():
    bj = Path(d) / 'board.json'
    if bj.exists():
        m = bj.stat().st_mtime
        if best is None or m > best[0]:
            best = (m, str(bj))
print(best[1] if best else '')
" "${hook_dir}" 2>/dev/null)"
    # No registered board with a live board.json → nothing to open; stay silent.
    [ -n "${board_path}" ] && [ -f "${board_path}" ] || exit 0
    # Fall through to the shared probe/open/digest block below.
  fi
fi

if [ -z "${board_path}" ]; then

  # Resolve the project root: git top-level if we're in a repo, else CWD.
  proj_root="$(cd "${PWD}" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)"
  [ -z "${proj_root}" ] && proj_root="${PWD}"

  # Launched from $HOME / "/" (the usual case — users start Claude in a terminal
  # at home, not inside a repo). We do NOT litter a board here. Instead we
  # ENUMERATE the projects the user actually worked in (from session history,
  # content-based — never a filesystem walk) and HAND OFF to Claude to draw a
  # picker. The user picks ONE; Claude runs bootstrap_project.sh for it. This
  # re-offers every session until the first board exists (the onboard_marker
  # check above exits before here once one does) — so it's never a one-shot miss
  # (the old .home-hint-shown gate is gone; that's what made the offer vanish on
  # the 2nd session). Opt out with BOARD_NO_AUTO_BOOTSTRAP=1.
  if [ "${proj_root}" = "${HOME}" ] || [ "${proj_root}" = "/" ]; then
    hook_dir="$(dirname "$0")"
    projects="$(BOARD_NO_AUTO_OPEN=1 python3 "${hook_dir}/discover2.py" \
                  --list-projects --top 5 --days 3 --format lines 2>/dev/null)"
    if [ -n "${projects}" ]; then
      cat <<EOF
<board-steward-session-start>
WorkBoard is installed and this session started in your home directory. The user
has NOT created their first board yet. From their Claude session history, these
are the projects they've actually worked in (most substantial first; tab =
path<TAB>label):

${projects}

ACTION (do this now, don't wait to be asked): FIRST print the question below as
a normal chat line (plain markdown — it renders visibly), THEN call
AskUserQuestion with the project options. The picker renders its own question
title in a color that is invisible on some terminal themes, so the chat line
above it is what the user actually reads; the picker's clickable options stay
visible regardless. Use THIS question text verbatim in both places (no markdown
bold/asterisks — literal ** renders invisible in the picker field):

  Which project should get your first WorkBoard? (You can add more later by asking to open a board for another project.)

Header: "Project". Make each listed project an option: label = the project name,
description = the FULL PATH + the "(ago, N sessions, N edits)" detail (e.g.
"~/code/acme-api — 12m ago, 638 sessions, 500 edits"). Exactly
one board on first run — they can add more later by asking "open a new workboard
for <project>". If their project isn't listed, they can type its path.

When they pick a project at PATH, create + open the board by running:

  bash "${hook_dir}/bootstrap_project.sh" "PATH"

That assigns the project's port, mines its history into a one-by-one fly-in,
opens the browser, and marks onboarding done. Do NOT pick for them.

When you announce the board, use THIS exact shape — relay verbatim, don't pad:

1. One line: the board is live + its port (http://127.0.0.1:<port>).
2. The FLY_ESTIMATE: line from the script's output, verbatim (the calculated
   "estimated time to finish + recommended action" message — short and sweet).
3. Then this block, exactly as written:

You're all set. A few things to know:
- **Add another board anytime** — just say "open a new workboard for <project>".
- **It remembers.** Close the tab, come back tomorrow — your board picks up right where you left off.
- **Just work normally.** Say things like "shipped X", "fixed Y", or "what's left?" and the cards move themselves.

What would you like to work on?

If asked what happens when they work while cards fly: nothing breaks — the board
keeps a server lock so writes serialize — but new edits can interleave with the
backfill and muddle ordering, so it's cleaner to let the fill settle.
</board-steward-session-start>
EOF
    else
      cat <<'HINT'
<board-steward-session-start>
WorkBoard is installed, but this session started in your home directory and no
prior project history was found to enumerate. To create your first board, cd into
a project folder and start Claude there, or ask: "set up a board for <path>".
</board-steward-session-start>
HINT
    fi
    exit 0
  fi

  # Bootstrap: spawn serve.py --bootstrap (creates board/ + mines history into a
  # one-by-one fly-in fill), install login autostart so it survives reboots, and
  # mark onboarded so this never re-fires. The browser auto-opens via the shared
  # block below once /health reports the server live.
  hook_dir="$(dirname "$0")"
  serve_py="${hook_dir}/serve.py"
  board_dir="${proj_root}/board"
  want_port="$(python3 -c "import sys; sys.path.insert(0, sys.argv[2]); import port_registry as pr; print(pr.assign(sys.argv[1]))" "${board_dir}" "${hook_dir}" 2>/dev/null || echo 7891)"
  if [ -f "${serve_py}" ]; then
    nohup python3 "${serve_py}" --project "${proj_root}" --port "${want_port}" --bootstrap \
      >"${proj_root}/.board-server.log" 2>&1 </dev/null &
    disown 2>/dev/null || true
    # Wait for the server to bind + write board.json (bootstrap_board is sync).
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
      curl -s --max-time 0.3 "http://127.0.0.1:${want_port}/health" >/dev/null 2>&1 && break
      sleep 0.4
    done
    # Survive reboots (the gap that killed the server today). Non-fatal.
    python3 "${hook_dir}/install_autostart.py" --project "${proj_root}" --port "${want_port}" \
      >/dev/null 2>&1 || true
    # Mark onboarded so we never auto-create a second board.
    mkdir -p "${HOME}/.board-steward" 2>/dev/null
    : > "${onboard_marker}"
    # Hand off to the shared digest + auto-open path below.
    board_path="${board_dir}/board.json"
  fi
  # If bootstrap didn't produce a board.json, revert to the original silence.
  [ -f "${board_path}" ] || exit 0
fi

project_dir="$(dirname "$(dirname "${board_path}")")"
board_dir="$(dirname "${board_path}")"
serve_py="$(dirname "$0")/serve.py"

# Resolve THIS board's designated port (#374) — per-project, stable, never
# collides. We probe/spawn only that port, so a session for project B can't
# latch onto project A's server just because A happens to hold 7891.
want_port="$(python3 -c "import sys; sys.path.insert(0, sys.argv[2]); import port_registry as pr; print(pr.assign(sys.argv[1]))" "${board_dir}" "$(dirname "$0")" 2>/dev/null || echo 7891)"

# Probe for THIS board's live server on its designated port.
server_health="$(curl -s --max-time 0.3 "http://127.0.0.1:${want_port}/health" 2>/dev/null)"
server_port="${want_port}"

# Auto-spawn if no server (covers users without launchd installed).
if [ -z "${server_health}" ] && [ -f "${serve_py}" ]; then
  lock="${project_dir}/board/.spawn.lock"
  now_ts="$(date +%s)"
  last_ts="$(stat -f %m "${lock}" 2>/dev/null || stat -c %Y "${lock}" 2>/dev/null || echo 0)"
  age=$((now_ts - last_ts))
  if [ "${age}" -gt 10 ]; then
    : > "${lock}"
    nohup python3 "${serve_py}" --project "${project_dir}" --port "${want_port}" \
      >/tmp/board-spawn.log 2>&1 </dev/null &
    disown 2>/dev/null || true
    sleep 0.8
    server_health="$(curl -s --max-time 0.3 "http://127.0.0.1:${want_port}/health" 2>/dev/null)"
    server_port="${want_port}"
  fi
fi

# Auto-open the board ONLY IF NO BROWSER IS CURRENTLY VIEWING IT (#377).
# The signal the user asked for ("check if ANY WB is opened; if not, open; else
# don't"): /health's sseClients = live browser connections. >0 → a tab is open,
# DON'T pop another (kills the "new tab on every Claude" spam). 0 → nothing is
# watching (no tab, or we just spawned the server) → open exactly one. To open an
# additional / different project's board, the user just asks. board.html SSE
# auto-reconnects, so a connected tab keeps sseClients>0 across server restarts.
# Honours BOARD_NO_AUTO_OPEN=1 for headless/CI/cron.
# #73 — open iff the board isn't already VISIBLE IN CHROME (a real tab), not the
# stale sseClients proxy that wrongly counted a backgrounded/closed connection as
# "open". Delegated to board_autoopen.sh (Chrome-tab check → sseClients fallback
# → opens in Chrome; honours BOARD_NO_AUTO_OPEN).
if [ -n "${server_health}" ]; then
  "$(dirname "$0")/board_autoopen.sh" "${server_port}" "${project_dir}" >/dev/null 2>&1 || true
fi

# Build digest: counts by column + last shipped card (with relative time).
digest="$(python3 - "${board_path}" <<'PY' 2>/dev/null
import json, sys
from datetime import datetime, timezone

with open(sys.argv[1]) as f:
    b = json.load(f)

cards = b.get("cards", [])
cols = {c["id"]: c.get("name", c["id"]) for c in b.get("columns", [])}

counts = {}
for c in cards:
    counts[c.get("column","?")] = counts.get(c.get("column","?"), 0) + 1

done = [c for c in cards if c.get("column") == "done" and c.get("doneAt")]
done.sort(key=lambda c: c.get("doneAt",""), reverse=True)

last = "(none)"
if done:
    top = done[0]
    ds = top.get("doneAt","")
    try:
        when = datetime.fromisoformat(ds.replace("Z","+00:00"))
        delta = datetime.now(timezone.utc) - when
        hrs = int(delta.total_seconds() // 3600)
        if hrs < 1:   ago = "<1h ago"
        elif hrs < 24: ago = f"{hrs}h ago"
        else:          ago = f"{hrs//24}d ago"
    except Exception:
        ago = ds[:10]
    last = f"#{top.get('num','?')} {top.get('code','')} ({ago})"

order = ["super-urgent", "mandatory", "ideas", "backlog", "inprogress", "blocked", "done"]
seen = set()
parts = []
for k in order:
    if counts.get(k, 0):
        parts.append(f"{cols.get(k,k)}: {counts[k]}")
        seen.add(k)
# Any custom columns not in order
for k, n in counts.items():
    if k not in seen and n:
        parts.append(f"{cols.get(k,k)}: {n}")

print(" · ".join(parts))
print(f"Last shipped: {last}")

# Launch-gate count (#91): items in super-urgent/mandatory with critical/mid prio.
# Surfaces blockers without loading the full board into context.
blocking = 0
for c in cards:
    if c.get("column") in ("super-urgent", "mandatory") and (c.get("priority") or "low") in ("critical", "mid"):
        blocking += 1
if blocking:
    print(f"🚨 LAUNCH-BLOCKING: {blocking} open · run `card.py prelaunch-check` before any launch/publish action")
PY
)"

# Extract live rev/cards from server health (UX sugar).
live_line=""
if [ -n "${server_health}" ]; then
  parsed="$(echo "${server_health}" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f\"{d.get('rev','?')}|{d.get('cards','?')}\")
except Exception:
    pass" 2>/dev/null)"
  if [ -n "${parsed}" ]; then
    rev="${parsed%%|*}"
    n="${parsed##*|}"
    live_line="Live at http://127.0.0.1:${server_port} (rev ${rev} · ${n} cards)"
  fi
fi

# Helper path Claude will see and can call.
card_py="$(dirname "$0")/card.py"

# Inline-extraction completion guard (#315): a leftover extraction_pending.json
# means a bootstrap staged digests but the completeness SWEEP was never run —
# never-miss cards may have been silently dropped. Single source of truth is
# sweep_status.py (pure-stdlib, no card.py import) so this stays self-contained
# and fast; it names the sweep explicitly so emit-then-delete can't skip recon.
pending_line=""
sweep_py="$(dirname "$0")/sweep_status.py"
if [ -f "${sweep_py}" ]; then
  pending_line="$(python3 "${sweep_py}" --board "${board_path}" --hook-line 2>/dev/null)"
fi

# Sign-off reconciliation backstop (#279): the previous session's Stop hook may
# have flagged un-carded work or open In-Progress cards. Surface it so this
# session closes the gap, then deletes the file.
recon_line=""
recon_file="$(dirname "${board_path}")/recon_pending.json"
if [ -f "${recon_file}" ]; then
  nreasons="$(python3 -c "import json;print(len(json.load(open('${recon_file}')).get('reasons',[])))" 2>/dev/null || echo "?")"
  recon_line="🔁 SIGN-OFF RECON PENDING: ${nreasons} item(s) in ${recon_file} — last session may have left work un-carded or cards stuck In-Progress. Review against your memory, create/move cards, then delete the file (stay-by-default)."
fi

cat <<MSG
<board-steward-session-start>
Board: ${board_path}
${live_line:-(server down — start: python3 $(dirname "$0")/serve.py --project ${project_dir})}
${digest}
${pending_line}
${recon_line}

Protocol: every ship/fix/defer → \`${card_py} add\` or \`${card_py} fly\` immediately (no batching). Status queries → \`card.py list\` or digest above, not memory. Detail → \`card.py show <num>\`. Never auto-Read board.json.
</board-steward-session-start>
MSG

exit 0
