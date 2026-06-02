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

# Stay silent for non-board projects.
[ -z "${board_path}" ] && exit 0

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
sse_clients="$(echo "${server_health}" | python3 -c "import sys,json
try: print(int(json.load(sys.stdin).get('sseClients',0)))
except Exception: print(0)" 2>/dev/null || echo 0)"
if [ -n "${server_health}" ] && [ "${sse_clients:-0}" -eq 0 ] && [ "${BOARD_NO_AUTO_OPEN:-0}" != "1" ]; then
  rm -f "${project_dir}"/board/.opened-* 2>/dev/null   # sweep stale #367 stamps
  url="http://127.0.0.1:${server_port}"
  if command -v open >/dev/null 2>&1; then open "${url}" >/dev/null 2>&1 &
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "${url}" >/dev/null 2>&1 &
  fi
  disown 2>/dev/null || true
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
