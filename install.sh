#!/usr/bin/env bash
# install.sh — one-command installer for WorkBoard / the board-steward plugin
# (a skills-dir plugin: it bundles the board-steward skill + the four hooks).
#
# The "one command to install" promise (VISION §Distribution + §3). A new user
# downloads this repo and runs:
#
#     ./install.sh                      # install skill + bootstrap a board in $(pwd)
#     ./install.sh --project ~/code/foo # ...in a specific project
#     ./install.sh --demo               # ISOLATED dry-run of the whole experience
#     ./install.sh --demo --harvest ~/code/foo   # ...filled (flying) from real history
#     ./install.sh --demo --harvest ~/code/foo --fill haiku  # ...filled AUTONOMOUSLY (no main-Claude step)
#
#   --fill haiku  how --harvest fills the board (haiku is the only engine):
#     haiku    = autonomous background workers emit the cards — one command, fills itself
#                (uses the user's existing Claude login, NO API key; fast + robust).
#     (inline and discover are retired — no longer selectable; engine code kept dormant.)
#
# What it does (in order):
#   1. Install the skill   → $CLAUDE_CONFIG_DIR/skills/board-steward (symlink to this repo)
#   2. Bootstrap a board   → serve.py --bootstrap (server + browser + the hourly
#                            two-tier FLY fill: last-1d fast → older backfills live)
#   3. Wire Claude hooks   → install_hooks.py --hook all (all four: SessionStart digest+auto-open,
#                            UserPromptSubmit nudge, PreToolUse flash, Stop sign-off backstop)
#   4. Autostart at login  → install_autostart.py (launchd/systemd/Task Scheduler)
#   5. Open the browser    → http://127.0.0.1:<port>
#
# --demo runs the full thing against an isolated $CLAUDE_CONFIG_DIR + a temp
# project + a spare port, skips global autostart, and REFUSES the real ports
# 7891/7892 — so it never touches a live setup. Prints teardown commands at the
# end. Idempotent: every sub-installer is safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="${REPO}/scripts"
PY="$(command -v python3 || command -v python)"

# Remember the REAL Claude config dir BEFORE --demo overrides it — the harvest
# extractor's `claude -p` calls need real credentials to authenticate (the
# isolated demo config dir is empty → every Haiku call would exit 1).
ORIG_CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-}"

# ---- defaults ----------------------------------------------------------------
PROJECT="$(pwd)"
PROJECT_EXPLICIT=0   # set when --project is passed → skip the #375 picker
PICK_DAYS=3          # history window the #375 project picker scans
PORT=7891
DEMO=0
OPEN_BROWSER=1
DO_AUTOSTART=1
DO_HOOKS=1
DO_SKILL=1
HARVEST=""          # if set: mine THIS real project's history into the (isolated) board
HARVEST_DAYS=2      # history window for --harvest
FILL="haiku"        # --harvest fill engine: haiku only (inline/discover retired, code kept dormant)

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; PROJECT_EXPLICIT=1; shift 2 ;;
    --port)    PORT="$2"; shift 2 ;;
    --demo)    DEMO=1; shift ;;
    --harvest) HARVEST="$2"; shift 2 ;;
    --harvest-days) HARVEST_DAYS="$2"; shift 2 ;;
    --fill)    FILL="$2"; shift 2 ;;
    --no-open) OPEN_BROWSER=0; shift ;;
    --no-autostart) DO_AUTOSTART=0; shift ;;
    --no-hooks)     DO_HOOKS=0; shift ;;
    --skip-skill)   DO_SKILL=0; shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# --harvest fills the board from a DIFFERENT project's history than where the
# board lives (two-project split) — the only safe way to demo the high-compute
# hourly fly fill without co-locating board.json inside the harvested repo.
if [ -n "$HARVEST" ]; then
  HARVEST="$(cd "$HARVEST" && pwd)" || { echo "✗ --harvest dir not found" >&2; exit 1; }
fi

say()  { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }

# ---- demo isolation ----------------------------------------------------------
DEMO_HOME=""
if [ "$DEMO" = "1" ]; then
  # Refuse the real ports so a live setup is never clobbered.
  if [ "$PORT" = "7891" ] || [ "$PORT" = "7892" ]; then PORT=7896; fi
  # Isolated config dir → hooks + skill land here, real ~/.claude untouched.
  DEMO_HOME="$(mktemp -d "${TMPDIR:-/tmp}/wb-demo.XXXXXX")"
  export CLAUDE_CONFIG_DIR="${DEMO_HOME}/claude"
  mkdir -p "${CLAUDE_CONFIG_DIR}"
  # ...but the haiku `claude -p` calls (install harvest AND serve.py bootstrap)
  # must auth against the user's REAL login — the demo config dir above has none,
  # so without this every call exits 1 → 0 cards. _LLM_ENV (hourly_common) reads
  # this and overrides CLAUDE_CONFIG_DIR for claude -p only. Empty → uses ~/.claude.
  export BOARD_REAL_CLAUDE_CONFIG_DIR="${ORIG_CLAUDE_CONFIG_DIR}"
  # Isolate the PORT REGISTRY too (#381). Without this the demo writes its temp
  # board dir into the REAL ~/.board-steward/*.json; the temp dir then lingers in
  # /tmp after teardown so it's never GC'd (Path.exists() stays true) and it
  # permanently hogs a port. Pointing both registry files into DEMO_HOME keeps
  # the real registry pristine AND gives the demo a clean slate, so its preferred
  # port is always free → serve.py binds exactly what install.sh expects.
  export BOARD_ASSIGNMENTS="${DEMO_HOME}/port-assignments.json"
  export BOARD_REGISTRY="${DEMO_HOME}/port-registry.json"
  # Temp project so board/ is created in throwaway space (never pollutes a real
  # dir). This gives the clean "empty start" new-user experience. To see the
  # "watch your board fill from history" moment instead, add --harvest <a real
  # project> (fills the demo board from that project's history).
  if [ "$PROJECT" = "$(pwd)" ]; then PROJECT="${DEMO_HOME}/project"; mkdir -p "$PROJECT"; fi
  DO_AUTOSTART=0   # never pollute launchd/systemd in a demo
  say "DEMO mode — isolated, nothing real is touched"
  echo "    config dir : ${CLAUDE_CONFIG_DIR}"
  echo "    project    : ${PROJECT}"
  echo "    port       : ${PORT}"
fi

echo
say "WorkBoard installer  (repo: ${REPO})"

# ---- 0. project picker (#375) ------------------------------------------------
# Users launch the terminal at $HOME, so PROJECT=$(pwd) resolves to $HOME — no
# project context, useless for a board. When that's the case (and we're
# interactive, not a demo, no explicit --project / --harvest), DISCOVER the
# projects the user actually worked in from session history (discover2's cwd
# signal — same convo source the task extractor uses; NO git-root walking, NO
# $HOME filesystem scan) and let them single-pick one. Running inside a real
# repo (PROJECT != $HOME) keeps the old cwd behaviour untouched.
if [ "$DEMO" = "0" ] && [ "$PROJECT_EXPLICIT" = "0" ] && [ -z "$HARVEST" ] \
   && [ -t 0 ] && [ "$PROJECT" = "$HOME" ]; then
  PICK_PATHS=(); PICK_LABELS=()
  while IFS=$'\t' read -r _p _label; do
    [ -n "$_p" ] || continue
    PICK_PATHS+=("$_p"); PICK_LABELS+=("$_label")
  done < <("$PY" "${SCRIPTS}/discover2.py" --list-projects \
             --days "$PICK_DAYS" --format lines 2>/dev/null || true)

  if [ "${#PICK_PATHS[@]}" -gt 0 ]; then
    echo
    say "You launched from \$HOME. Recent projects from your history:"
    _i=1
    for _label in "${PICK_LABELS[@]}"; do
      printf '   %d) %s\n' "$_i" "$_label"; _i=$((_i + 1))
    done
    printf '   Pick a project to open a board for [1-%d, or paste a path] (default 1): ' "${#PICK_PATHS[@]}"
    read -r _ans || _ans=""
    if [ -z "$_ans" ]; then
      PROJECT="${PICK_PATHS[0]}"
    elif printf '%s' "$_ans" | grep -Eq '^[0-9]+$' \
         && [ "$_ans" -ge 1 ] && [ "$_ans" -le "${#PICK_PATHS[@]}" ]; then
      PROJECT="${PICK_PATHS[$((_ans - 1))]}"
    else
      PROJECT="${_ans/#\~/$HOME}"   # treat as a literal path (expand leading ~)
    fi
    ok "project → ${PROJECT}"
  else
    warn "no recent projects found in history — using \$HOME (${PROJECT})"
  fi
fi

# ---- 1. skill ----------------------------------------------------------------
if [ "$DO_SKILL" = "1" ]; then
  CFG_BASE="${CLAUDE_CONFIG_DIR:-${HOME}/.claude}"
  SKILL_DIR="${CFG_BASE}/skills/board-steward"
  if [ -e "$SKILL_DIR" ] && [ ! -L "$SKILL_DIR" ]; then
    warn "skill dir exists and is not a symlink — leaving it as-is: ${SKILL_DIR}"
  else
    mkdir -p "$(dirname "$SKILL_DIR")"
    if ln -sfn "$REPO" "$SKILL_DIR" 2>/dev/null && [ -L "$SKILL_DIR" ]; then
      ok "skill linked → ${SKILL_DIR}"
    else
      # Symlinks need admin/Developer Mode on Windows (Git Bash) — fall back to a
      # plain copy so install still works. Note: a copy won't auto-track repo
      # edits; re-run install.sh after pulling updates.
      rm -rf "$SKILL_DIR" 2>/dev/null || true
      cp -R "$REPO" "$SKILL_DIR"
      ok "skill copied → ${SKILL_DIR} (symlink unavailable; re-run install.sh to update)"
    fi
  fi
else
  warn "skipping skill install (--skip-skill)"
fi

# ---- 2. bootstrap board + server --------------------------------------------
# Resolve THIS board's sticky designated port from the registry BEFORE we launch
# or health-check (#381). port_registry bumps the preferred port when it's
# already designated to another project (multi-project, or a leftover demo);
# serve.py binds whatever the registry returns, so install.sh must learn that
# SAME port now — otherwise it polls the wrong one and falsely reports "server
# did not come up." assign() is idempotent + sticky, so serve.py's own assign()
# returns this exact value. Board dir / scripts / port passed as argv so a
# project path with spaces (e.g. "Edu Platform") can't break the quoting.
RESOLVED_PORT="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[2]); import port_registry as p; print(p.assign(sys.argv[1], preferred=int(sys.argv[3])))' "${PROJECT}/board" "${SCRIPTS}" "${PORT}" 2>/dev/null || true)"
if printf '%s' "$RESOLVED_PORT" | grep -Eq '^[0-9]+$'; then PORT="$RESOLVED_PORT"; fi
say "bootstrapping board in ${PROJECT} (port ${PORT})"
# Stop any stale server on this exact port first (demo re-runs).
if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
  lsof -ti "tcp:${PORT}" | xargs kill 2>/dev/null || true
  sleep 0.4
fi
# With --harvest the board lives in $PROJECT but history is mined from a
# different repo, so suppress serve.py's own (empty-project) discovery and run
# the extractor ourselves below.
BOOT_DISCOVER=""
[ -n "$HARVEST" ] && BOOT_DISCOVER="--no-discover"
# With --harvest the board lives in the throwaway $PROJECT dir, so the title
# would auto-derive to that dir's basename (e.g. "project"). Name it after the
# harvested source so the board reads "WorkBoard — <harvested project>".
BOOT_TITLE=""
[ -n "$HARVEST" ] && BOOT_TITLE="--title $(basename "$HARVEST")"
nohup "$PY" "${SCRIPTS}/serve.py" --project "$PROJECT" --port "$PORT" --bootstrap $BOOT_DISCOVER $BOOT_TITLE \
  >"${PROJECT}/.board-server.log" 2>&1 &
# Wait for /health.
for _ in $(seq 1 25); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done
SERVER_OK=0
if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  SERVER_OK=1
  CARDS="$(curl -s "http://127.0.0.1:${PORT}/health" | "$PY" -c 'import sys,json;print(json.load(sys.stdin).get("cards","?"))' 2>/dev/null || echo '?')"
  ok "server live at http://127.0.0.1:${PORT}  (${CARDS} cards)"
else
  warn "server did not come up — see ${PROJECT}/.board-server.log"
fi

# Open the browser NOW (before the harvest fill) so the user watches cards fly
# in live, then run the high-compute hourly extractor against the real history.
if [ -n "$HARVEST" ] && [ "$SERVER_OK" = "1" ]; then
  URL="http://127.0.0.1:${PORT}"
  if [ "$OPEN_BROWSER" = "1" ]; then
    if command -v open >/dev/null 2>&1; then open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"; fi
    OPEN_BROWSER=0   # don't reopen at the end
  fi
  # haiku is the only fill engine; inline/discover are retired (code kept dormant).
  if [ "$FILL" != "haiku" ]; then
    warn "--fill '${FILL}' is retired (inline/discover removed) — using haiku"
    FILL="haiku"
  fi
  # HAIKU: autonomous background workers (claude -p) emit the cards themselves —
  # one command, board fills + HUD ticks with no main-Claude step. Costs Haiku.
  say "filling board from ${HARVEST} history via HAIKU (autonomous — no main-Claude step)"
  # claude -p authenticates via BOARD_REAL_CLAUDE_CONFIG_DIR (set in the --demo
  # block) → the user's real login, not the empty isolated demo config dir.
  # #327 --tier-fly: days>1 → watched tier-1 (last 1d, lifecycle flights)
  # flies in, THEN the faster "speeding up" tier-2 backfill — instead of one
  # flat 63-chunk pass that pops cards in without flying.
  "$PY" "${SCRIPTS}/hourly_extractor.py" \
    --project "$HARVEST" --board "${PROJECT}/board/board.json" --port "$PORT" \
    --days "$HARVEST_DAYS" --bucket-min 30 --chunk-size 2 --recent-first --mode haiku --tier-fly \
    || warn "harvest haiku fill reported an issue (non-fatal)"
  ok "haiku fill complete — board filled autonomously (no main-Claude step needed)"
fi

# ---- 3. hooks ----------------------------------------------------------------
if [ "$DO_HOOKS" = "1" ]; then
  say "wiring Claude Code hooks (SessionStart + UserPromptSubmit + PreToolUse + Stop)"
  "$PY" "${SCRIPTS}/install_hooks.py" --hook all >/dev/null && ok "hooks installed in ${CLAUDE_CONFIG_DIR:-${HOME}/.claude}/settings.json"
else
  warn "skipping hooks (--no-hooks)"
fi

# ---- 4. autostart ------------------------------------------------------------
if [ "$DO_AUTOSTART" = "1" ]; then
  say "registering autostart at login"
  "$PY" "${SCRIPTS}/install_autostart.py" --project "$PROJECT" --port "$PORT" >/dev/null \
    && ok "autostart registered (login → server on :${PORT})" \
    || warn "autostart step reported an issue (non-fatal)"
else
  [ "$DEMO" = "1" ] && warn "skipping autostart (demo)" || warn "skipping autostart (--no-autostart)"
fi

# ---- 5. open browser ---------------------------------------------------------
URL="http://127.0.0.1:${PORT}"
if [ "$OPEN_BROWSER" = "1" ] && [ "$SERVER_OK" = "1" ]; then
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi
fi

echo
if [ "$SERVER_OK" = "1" ]; then
  ok "Done. Board live at ${URL}"
else
  warn "Install incomplete — board server did not come up (see ${PROJECT}/.board-server.log)"
fi
if [ "$DEMO" = "1" ]; then
  echo
  say "DEMO teardown when you're finished:"
  echo "    lsof -ti tcp:${PORT} | xargs kill        # stop the demo server"
  echo "    rm -rf '${DEMO_HOME}'                    # remove demo config + project"
  echo "  (your real ~/.claude, boards, and launchd jobs were never touched)"
else
  echo "    Point Claude at this project and it'll keep the board in sync as you work."
fi
