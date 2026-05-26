#!/usr/bin/env bash
# board-steward UserPromptSubmit hook
#
# Injects a board-protocol reminder into Claude's context BEFORE it responds
# to a new user message. Fires only when (a) a board/board.json exists in the
# CWD tree, or (b) a board server is live on a common local port.
#
# Output goes straight into Claude's context. Exit 0 always (non-blocking).
# Cost: <50ms in the common case.

set +e
set -u

# Walk up from CWD looking for board/board.json (max 8 levels).
# This is the ONLY trigger — a server elsewhere on the machine shouldn't
# leak protocol chatter into non-board projects.
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

# Stay silent for non-board projects.
if [ -z "${board_path}" ]; then
  exit 0
fi

# Optional: probe common ports for a live server matching THIS board.
# (Pure UX sugar — adds rev to the message if we can confirm a server is up.)
server_hint=""
for port in 7891 7892 7893 7894 7895; do
  health="$(curl -s --max-time 0.3 "http://127.0.0.1:${port}/health" 2>/dev/null)"
  if [ -n "${health}" ]; then
    rev="$(echo "${health}" | python3 -c "import sys,json
try: d=json.load(sys.stdin); print(d.get('rev','?'))
except: pass" 2>/dev/null)"
    if [ -n "${rev}" ] && [ "${rev}" != "?" ]; then
      server_hint=" @ :${port} (rev ${rev})"
      break
    fi
  fi
done

# Auto-spawn server if board found but no live server on :7891.
# Covers the "skill installed but launchd not configured yet" case + crash recovery.
# Lock file with 10s window prevents double-spawn on rapid prompts.
if [ -z "${server_hint}" ]; then
  project_dir="$(dirname "$(dirname "${board_path}")")"
  serve_py="$(dirname "$0")/serve.py"
  lock="${project_dir}/board/.spawn.lock"
  now_ts="$(date +%s)"
  last_ts="$(stat -f %m "${lock}" 2>/dev/null || stat -c %Y "${lock}" 2>/dev/null || echo 0)"
  age=$((now_ts - last_ts))
  if [ -f "${serve_py}" ] && [ "${age}" -gt 10 ]; then
    : > "${lock}"
    nohup python3 "${serve_py}" --project "${project_dir}" --port 7891 \
      >/tmp/board-spawn.log 2>&1 </dev/null &
    disown 2>/dev/null || true
    server_hint=" @ :7891 (spawning…)"
  fi
fi

# Best-effort: read last card-added timestamp from telemetry.
TELEMETRY="${HOME}/.agents/skills/board-steward/telemetry/events.jsonl"
[ -f "${TELEMETRY}" ] || TELEMETRY="$(dirname "$0")/../telemetry/events.jsonl"
last_card_ts=""
if [ -f "${TELEMETRY}" ]; then
  last_card_ts="$(grep '"cards_added"' "${TELEMETRY}" 2>/dev/null \
                  | python3 -c "import sys,json
last=''
for line in sys.stdin:
    try:
        e=json.loads(line)
        if e.get('cards_added',0)>0: last=e.get('ts','')
    except: pass
print(last)" 2>/dev/null)"
fi

cat <<MSG
<board-steward-protocol>
A live work board is tracking this project${server_hint}.

MANDATORY before responding:
  - If your IMMEDIATE prior assistant turn shipped / deployed / completed / fixed / deferred anything that isn't already on the board, run \`card.py add\` or \`card.py move\` NOW. Don't batch — one update per shipped unit.
  - If you only investigated or read, do nothing.

Why this hook exists: the user must NEVER have to ask "did you update the board?" — that question is a failure of this protocol.

Last card-added event: ${last_card_ts:-none recorded}
Board: ${board_path:-(server-only)}
</board-steward-protocol>
MSG

exit 0
