#!/usr/bin/env bash
# Auto-open the board in the browser - the ONE gate every caller goes through
# (SessionStart hook, bootstrap_project.sh, card.py board-new).
#
# History: #73 asked Chrome (AppleScript) whether a tab shows the board instead
# of trusting a stale SSE count; #122 added the burst lock + cooldown; #836
# matched localhost and 127.0.0.1 alike; #907 made the passive open once per
# day. #959 moved the decision into _autoopen.py so it is testable and TRACKED:
# every decision (open, skip, defer, error) is one JSON line in
# ~/.board-steward/autoopen.jsonl - read it with `card.py autoopen-log`.
#
# Policy ("one WorkBoard per Chrome", #959): a passive caller opens nothing
# while ANY board tab is visible in Chrome (any port, any board, 127.0.0.1 or
# localhost); its once-per-day gate is global across boards; a compaction never
# opens; Chrome not running is handled by a deferred pass that lets Chrome
# restore its session before deciding. Explicit callers (BOARD_OPEN_EXPLICIT=1)
# asked for THIS board: per-port match, no day gate, cooldown + lock still apply.
#
# Usage: board_autoopen.sh <port> [project_dir] [session_id]
# Env:   BOARD_NO_AUTO_OPEN=1 (headless/CI/cron)  BOARD_OPEN_EXPLICIT=1
#        BOARD_OPEN_CALLER=<tag>  BOARD_OPEN_SOURCE=<SessionStart source>
set -u
here="$(cd "$(dirname "$0")" && pwd)"
exec python3 "${here}/_autoopen.py" "$@"
