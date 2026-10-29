#!/usr/bin/env bash
# autocreate_board.sh <project_root> [session_id]
#
# #960 - the background half of an automatic board. The SessionStart hook
# decides a new project should get a board (_autocreate.py decide), launches
# this detached, and returns at once so session start stays fast.
#
#   1. bootstrap_project.sh creates + serves the board (sticky port, history
#      fly-in, login autostart, last-active pointer), with its own browser open
#      suppressed.
#   2. serve.py announces the new board to every other live board server
#      (POST /boards/notify), so an already-open WorkBoard tab shows it in the
#      project switcher with a "New board" notice, no reload.
#   3. A browser tab is opened ONLY when no WorkBoard tab is open at all (any
#      board, any port). Opening goes through the existing board_autoopen.sh
#      gate (explicit mode, like bootstrap) so its dedupe and BOARD_NO_AUTO_OPEN
#      still apply.
set +e
set -u

root="${1:?usage: autocreate_board.sh <project_root> [session_id]}"
sid="${2:-}"
hook_dir="$(cd "$(dirname "$0")" && pwd)"

# Remember the user's own open preference before we silence bootstrap's open.
user_no_open="${BOARD_NO_AUTO_OPEN:-0}"

marker="${HOME}/.board-steward/.onboarded"
had_marker=0; [ -f "${marker}" ] && had_marker=1

port="$(BOARD_NO_AUTO_OPEN=1 bash "${hook_dir}/bootstrap_project.sh" "${root}" | tail -n 1)"

# #556 - bootstrap_project.sh marks the user onboarded unconditionally. If no
# board.json came out of it, undo that for a user who had no board before, so
# their next $HOME session still gets the first-board picker.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -f "${root}/board/board.json" ] && break
  sleep 0.5
done
if [ ! -f "${root}/board/board.json" ]; then
  [ "${had_marker}" = "0" ] && rm -f "${marker}"
  echo "autocreate: bootstrap failed for ${root}" >&2
  exit 1
fi
case "${port}" in ''|*[!0-9]*) echo "autocreate: no port for ${root}" >&2; exit 1 ;; esac
echo "autocreate: board for ${root} on port ${port}"

[ "${user_no_open}" = "1" ] && exit 0

# Any WorkBoard tab already open (other than this new board's own)? Then the
# new board shows up in that tab's switcher instead of a new tab.
if python3 "${hook_dir}/_autocreate.py" any-tab --exclude "${port}"; then
  echo "autocreate: a WorkBoard tab is already open; not opening another"
  exit 0
fi

BOARD_OPEN_EXPLICIT=1 BOARD_OPEN_CALLER="autocreate" \
  "${hook_dir}/board_autoopen.sh" "${port}" "${root}" "${sid}" >/dev/null 2>&1 || true
exit 0
