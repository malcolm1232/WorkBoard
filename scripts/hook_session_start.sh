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

# #611 — capture the hook payload ONCE (stdin is consumable once) and extract the
# session_id. SessionStart hooks receive {"session_id":..,"source":startup|resume|..}
# on stdin. On RESUME the id matches the session that did the last-active writes, so
# passing it into port_registry.get_active() reopens THIS session's own board even
# if another concurrent session touched a different board meanwhile. Empty / non-JSON
# stdin → empty id → the fresh-session global/mtime fallback (today's behaviour).
# Never errors (|| true + except) so startup + exit 0 are preserved.
hook_stdin="$(cat 2>/dev/null || true)"
session_id="$(printf '%s' "${hook_stdin}" | python3 -c '
import sys, json
try:
    print(json.load(sys.stdin).get("session_id", "") or "")
except Exception:
    pass
' 2>/dev/null || true)"

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
sid = sys.argv[2] if len(sys.argv) > 2 else ''
active = pr.get_active(sid or None)   # #611 — this session's board, else global
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
" "${hook_dir}" "${session_id}" 2>/dev/null)"
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

ACTION (do this now, don't wait to be asked):

  • If there is EXACTLY ONE project listed above → do NOT draw a picker or wait.
    Auto-open it: run \`bash "${hook_dir}/bootstrap_project.sh" "<that one PATH>"\`
    straight away (default 2-day window), then go to the announcement below. One
    option means there's nothing to choose.

  • If there are TWO OR MORE projects → PRINT them as a
NUMBERED LIST in a normal chat message (plain markdown — it renders on EVERY
terminal theme). Do NOT call AskUserQuestion: its question-title field is
invisible on any theme that mismatches the terminal background (e.g. the default
dark theme on a light terminal, or vice-versa), which silently hides the
question — so we use plain text the user can always read. Then ask the user to
reply with the NUMBER (or a path). Use this shape verbatim (no bold/asterisks):

  Which project should get your first WorkBoard? Reply with a number (or paste a path):
    1. <project name> — <FULL PATH>  (<ago, N sessions, N edits>)
    2. <project name> — <FULL PATH>  (<ago, …>)
    …
  You can add more boards later by asking to open a board for another project.
  If your project isn't listed, just paste its path.

Wait for their reply; do NOT pick for them. When they reply, resolve their
number/path to the project PATH and create + open the board by running:

  bash "${hook_dir}/bootstrap_project.sh" "PATH"

That assigns the project's port, mines its history into a one-by-one fly-in,
opens the browser, and marks onboarding done. Do NOT pick for them.

Use the DEFAULT 2-day window — run it with just the PATH. 2 days is the good
first experience (recent, relevant work). Only append a days argument (e.g.
bootstrap_project.sh "PATH" 14) if the user EXPLICITLY asks to mine older / more
history — never bump it yourself; a wide window floods the first board with stale,
less-relevant cards.

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
    # env -u CLAUDECODE: the bootstrap fill + its end-of-fill reconcile_sweep run
    # as subprocesses of this server; unset CLAUDECODE so they take the autonomous
    # Haiku path, not the in-session prose recon_pending path (#recon).
    env -u CLAUDECODE nohup python3 "${serve_py}" --project "${proj_root}" --port "${want_port}" --bootstrap \
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
    # Hand off to the shared digest + auto-open path below.
    board_path="${board_dir}/board.json"
    # Don't ALSO run the SessionStart recon this turn: the bootstrap fill is
    # still streaming cards in the background, and its own end-of-fill
    # reconcile_sweep already covers the freshly-mined history. A second
    # concurrent recon would race the fill's card.py writes (lost-update on the
    # full-replace POST) for no added signal. Resumed sessions still reconcile.
    just_bootstrapped=1
  fi
  # #556 — mark onboarded ONLY after bootstrap actually produced a board.json.
  # Writing the marker before this check (old order) stranded the user "onboarded
  # but boardless" on a failed bootstrap: the picker never re-fired and every later
  # session exited silently. No board.json → exit WITHOUT the marker, so the next
  # session re-offers the picker.
  if [ -f "${board_path}" ]; then
    mkdir -p "${HOME}/.board-steward" 2>/dev/null
    : > "${onboard_marker}"
  else
    exit 0
  fi
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
  # #836 — pass THIS session's id so the opened tab carries ?sid (top-pin). The
  # id was parsed from stdin above; board_autoopen reads it from $3 (or the env
  # var as fallback). Without this, hook-opened tabs were sid-less while agent-run
  # board-new tabs had one → the "some have sid, some don't" inconsistency.
  "$(dirname "$0")/board_autoopen.sh" "${server_port}" "${project_dir}" "${session_id}" >/dev/null 2>&1 || true
  # #505 — fire a one-shot 'session refresh' divider into the Logs HUD so a fresh
  # session is visually separated from the previous one's activity. Detached +
  # bounded so it never delays startup; waits (≤4s) for a connected SSE client
  # because the pulse isn't retained — a divider fired into an empty room is lost
  # (board_autoopen may have JUST opened the tab, which connects asynchronously).
  # Skip during a bootstrap session: the fresh first-run board has no prior
  # activity to separate from, and the divider would just clutter the fly-in.
  if [ "${just_bootstrapped:-0}" != "1" ]; then
    (
      for _ in $(seq 1 16); do
        h="$(curl -s --max-time 0.3 "http://127.0.0.1:${server_port}/health" 2>/dev/null)"
        case "${h}" in
          *'"sseClients": '[1-9]*)
            curl -s --max-time 0.3 "http://127.0.0.1:${server_port}/divider" >/dev/null 2>&1
            break ;;
        esac
        sleep 0.25
      done
    ) >/dev/null 2>&1 &
    disown 2>/dev/null || true
  fi
fi

# Partial-bootstrap recovery only (SessionStart, #800): the per-session Haiku
# reconcile sweep is intentionally GONE — a fresh session no longer moves cards
# around autonomously. What remains is the dropped-card SELF-HEAL: if a prior
# bootstrap fill hard-failed a bucket and dropped its cards (state partial:true /
# failed_buckets), re-create them now. This is a NO-OP on a healthy board (no
# partial state → fast exit, no Haiku, no board write). Runs DETACHED so the
# digest below stays instant; recovered cards animate on the live board over SSE.
# CLAUDECODE is UNSET so the recovery extraction takes the cheap Haiku path, not
# the prose recon_pending path (reserved for the in-session main agent).
# Opt out with BOARD_NO_RECON=1 (CI/headless/demo) for a one-off, or drop a
# persistent per-board marker file board/.no-live-recon to silence this for THIS
# board forever (#516). Bootstrap itself is a separate path (serve.py
# --bootstrap) and is unaffected by either opt-out.
if [ "${just_bootstrapped:-0}" != "1" ] && [ "${BOARD_NO_RECON:-0}" != "1" ] \
   && [ ! -f "$(dirname "${board_path}")/.no-live-recon" ] && [ -f "${board_path}" ]; then
  extractor="$(dirname "$0")/hourly_extractor.py"
  if [ -f "${extractor}" ]; then
    env -u CLAUDECODE nohup python3 "${extractor}" \
      --project "${project_dir}" --board "${board_path}" --backfill-only \
      >"${project_dir}/.board-backfill.log" 2>&1 </dev/null &
    disown 2>/dev/null || true
  fi
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
