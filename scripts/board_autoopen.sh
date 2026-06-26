#!/usr/bin/env bash
# #73 — Auto-open the board in the browser UNLESS it's already visible in Google
# Chrome. Replaces the old sseClients gate: a *stale* SSE connection (a closed/
# backgrounded tab whose socket hadn't torn down) counted as "open" even when no
# Chrome tab showed the board — so a freshly-launched Claude skipped the open and
# the user saw nothing ("opened in backend, not in Chrome"). We now ask Chrome
# directly (AppleScript) whether a tab has the board URL, and open iff it does
# not. Opening prefers Chrome (the user's browser).
#
# Falls back to the sseClients heuristic when Chrome state can't be read
# (non-macOS, no osascript, or Automation permission denied) so non-Mac/remote
# users keep working. Honours BOARD_NO_AUTO_OPEN=1 (headless/CI/cron).
#
# Usage: board_autoopen.sh <port> [project_dir] [session_id]
set -u
port="${1:?need port}"
project_dir="${2:-}"
# #836 — explicit session id (3rd arg) beats the env var. The SessionStart hook
# parses session_id from stdin but doesn't export it as CLAUDE_CODE_SESSION_ID,
# so hook-opened tabs used to be sid-less while agent-run board-new tabs (which
# inherit the env var) had one. Accepting it positionally makes the ?sid reliable
# regardless of how we were invoked.
sid="${3:-${CLAUDE_CODE_SESSION_ID:-}}"
[ "${BOARD_NO_AUTO_OPEN:-0}" = "1" ] && exit 0

url="http://127.0.0.1:${port}"
# #836 — host-agnostic dedupe: a tab opened as localhost:PORT must count as the
# same board as one opened as 127.0.0.1:PORT, else the Chrome check below misses
# it and we open a duplicate. We always OPEN with 127.0.0.1, but MATCH either.
match="127.0.0.1:${port}"
match_alt="localhost:${port}"

# #608 — bind the opened board window to THIS Claude Code session so it can pin
# that session's active card to the top of In-Progress (other sessions' cards
# still pulse, just aren't pinned here). match stays host:port-only so we don't
# spawn a duplicate window per session; if a board tab is already open it's
# reused (its opener owns the pin; the page falls back to most-recently-claimed
# when its ?sid session has ended). Empty sid → no param, pure fallback behavior.
open_url="${url}"
[ -n "${sid}" ] && open_url="${url}/?sid=${sid}"

# Don't open a dead URL — the board's server must be live.
curl -s --max-time 0.4 "${url}/health" >/dev/null 2>&1 || exit 0

# --- Dedupe concurrent / rapid opens (#122) ---------------------------------
# Two failure modes this guards against:
#  (1) BURST ("7 tabs at once"): several invocations — multiple session-starts
#      during bootstrap replay — race the async `open` below. Each queries
#      Chrome before any open has rendered a visible tab, so ALL of them open.
#      An atomic mkdir lock lets exactly one proceed; the losers exit.
#  (2) PERIODIC: a fresh open moments after a real one is suppressed by a short
#      cooldown stamp, covering the window where the just-opened tab isn't yet
#      visible to the Chrome/SSE check (SSE keepalive flap, async render).
state_dir="${HOME}/.board-steward"
mkdir -p "${state_dir}" 2>/dev/null || true
stamp="${state_dir}/.opened-${port}"
lock="${state_dir}/.opening-${port}.lock"
cooldown=12

# Cooldown: we opened (or tried) very recently → don't open again.
if [ -f "${stamp}" ]; then
  now="$(date +%s 2>/dev/null || echo 0)"
  was="$(stat -f %m "${stamp}" 2>/dev/null || stat -c %Y "${stamp}" 2>/dev/null || echo 0)"
  [ $(( now - was )) -lt "${cooldown}" ] && exit 0
fi

# Reap a stale lock (a previous invocation killed before releasing its trap),
# else a dead lockdir would block every future open forever.
if [ -d "${lock}" ]; then
  now="$(date +%s 2>/dev/null || echo 0)"
  lk="$(stat -f %m "${lock}" 2>/dev/null || stat -c %Y "${lock}" 2>/dev/null || echo 0)"
  [ $(( now - lk )) -ge 30 ] && rmdir "${lock}" 2>/dev/null || true
fi

# Lock: serialize the check-and-open so a burst collapses to a single open.
# mkdir is atomic across processes; the loser exits rather than piling on.
mkdir "${lock}" 2>/dev/null || exit 0
trap 'rmdir "${lock}" 2>/dev/null || true' EXIT

# --- Is the board already visible in Chrome? --------------------------------
# chrome_state: 0=yes (a tab has it → skip), 1=no (open it), 2=unknown (fallback)
chrome_state=2
if command -v osascript >/dev/null 2>&1; then
  if pgrep -x "Google Chrome" >/dev/null 2>&1; then
    # Query via System Events so we never LAUNCH Chrome just to inspect it.
    res="$(osascript <<AS 2>/dev/null
tell application "System Events"
  if (exists process "Google Chrome") then
    tell application "Google Chrome"
      repeat with w in windows
        repeat with t in tabs of w
          if ((URL of t) contains "${match}") or ((URL of t) contains "${match_alt}") then return "yes"
        end repeat
      end repeat
    end tell
  end if
end tell
return "no"
AS
)"
    rc=$?
    if [ "$rc" -ne 0 ]; then chrome_state=2          # AS error / permission denied
    elif [ "$res" = "yes" ]; then chrome_state=0     # a Chrome tab has the board
    else chrome_state=1                              # Chrome up, no such tab
    fi
  else
    chrome_state=1                                   # Chrome not running → open it
  fi
fi

should_open=0
case "$chrome_state" in
  0) should_open=0 ;;
  1) should_open=1 ;;
  2) # Unknown (no AppleScript / non-mac) → ask the server. #150: a single
     # /health read, robust to SSE flaps. A tab is "present" if it has a live SSE
     # client (sseClients>0) OR one connected within the last 20s — an open board
     # tab auto-reconnects within ~3s of any flap, so the durable lastSseConnect
     # age keeps the answer stable where the raw instantaneous count used to blip
     # to 0 and spawn a duplicate tab. Open only if genuinely no recent viewer
     # (or /health is unreachable).
     present="$(curl -s --max-time 0.6 "${url}/health" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    n = int(d.get('sseClients', 0) or 0)
    now = int(d.get('nowMs', 0) or 0)
    last = int(d.get('lastSseConnectMs', 0) or 0)
    fresh = bool(now and last and (now - last) < 20000)
    print('1' if (n > 0 or fresh) else '0')
except Exception:
    print('0')
" 2>/dev/null || echo 0)"
     [ "${present:-0}" != "1" ] && should_open=1 ;;
esac

[ "$should_open" -eq 1 ] || exit 0

# Arm the cooldown (#122): stamp NOW, before the async `open`, so any invocation
# in the next ${cooldown}s — while this tab is still rendering and not yet
# visible to the Chrome/SSE check — sees a fresh stamp and skips.
touch "${stamp}" 2>/dev/null || true

# Sweep stale #367 open-stamps for this project, then open (prefer Chrome).
[ -n "$project_dir" ] && rm -f "${project_dir}"/board/.opened-* 2>/dev/null
if command -v open >/dev/null 2>&1; then
  { open -a "Google Chrome" "${open_url}" 2>/dev/null || open "${open_url}" 2>/dev/null ; } &
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "${open_url}" >/dev/null 2>&1 &
fi
disown 2>/dev/null || true
exit 0
