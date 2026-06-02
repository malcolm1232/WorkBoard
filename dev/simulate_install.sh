#!/usr/bin/env bash
# simulate_install.sh — replay the first-user install moment for board-steward.
#
# Spawns an isolated board server on a chosen port, then replays a chosen
# project's real session history into it so the user opens the browser and
# watches the board fill + FLY itself from their actual work.
#
# DEFAULT (canonical demo, card #264): --replay-mode hourly --chunk-size 2
# --hourly-show-lifecycle. Buckets the history by the hour, sends each bucket
# to `claude -p haiku`, gets back high-quality WORK-UNIT cards, and flies each
# task→inprogress→done. Compute-heavy by design (one Haiku call per bucket) —
# kept for architectural fidelity; compute-light rework tracked as #264.
#
# Fallback: --replay-mode bulk = no-API-key discover2 heuristic (lower quality;
# "plops" rather than the LLM voice).
#
# Idempotent: tearing down any existing sim on the same port + wiping the
# sim dir before re-running is safe. Real boards on :7891/:7892 are never
# touched (this script refuses those ports).
#
# Usage:
#   scripts/simulate_install.sh                         # defaults: HFTAgents, :7894, hourly chunk=2
#   scripts/simulate_install.sh --project PATH
#   scripts/simulate_install.sh --project PATH --port 7895
#   scripts/simulate_install.sh --project PATH --sim-dir ~/Desktop/my-sim
#   scripts/simulate_install.sh --no-open               # skip browser open
#   scripts/simulate_install.sh --days 14               # history reach
#   scripts/simulate_install.sh --replay-mode bulk      # no-API-key fallback
#
set -euo pipefail

# ---- defaults ----------------------------------------------------------------
PROJECT="${HOME}/Desktop/QuantifyMe/HFTAgents"
PORT=7894
SIM_DIR=""                          # default derived below from date stamp
DAYS=7
MAX=20
OPEN_BROWSER=1
PROFILE="software"
LIFECYCLE=0                         # opt-in (--lifecycle) synthetic demo card; hourly cards fly themselves
LIFECYCLE_INTERVAL=2                # seconds between phases
LEGACY_DISCOVER=0                   # 1 = use old discover.py (session-shaped)
REPLAY_MODE="haiku"                 # haiku = claude -p fly (only engine; inline/discover retired, code kept dormant)
HOURLY_MAX_BUCKETS=0                # 0 = all hours in --days window
HOURLY_SHOW_LIFECYCLE=1             # 1 = play task→ip→done per card (the fly)
HOURLY_BUCKET_MIN=30                # bucket size in minutes
HOURLY_CHUNK_SIZE=2                 # buckets per LLM call (the #217/#218 demo config)
HOURLY_DATE=""                      # YYYY-MM-DD UTC pin (empty = no pin)

# ---- arg parsing -------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)  PROJECT="$2"; shift 2 ;;
    --port)     PORT="$2"; shift 2 ;;
    --sim-dir)  SIM_DIR="$2"; shift 2 ;;
    --days)     DAYS="$2"; shift 2 ;;
    --max)      MAX="$2"; shift 2 ;;
    --profile)  PROFILE="$2"; shift 2 ;;
    --no-open)  OPEN_BROWSER=0; shift ;;
    --no-lifecycle) LIFECYCLE=0; shift ;;
    --lifecycle-interval) LIFECYCLE_INTERVAL="$2"; shift 2 ;;
    --legacy-discover) LEGACY_DISCOVER=1; shift ;;
    --replay-mode) REPLAY_MODE="$2"; shift 2 ;;
    --lifecycle) LIFECYCLE=1; shift ;;
    --hourly-max-buckets) HOURLY_MAX_BUCKETS="$2"; shift 2 ;;
    --hourly-show-lifecycle) HOURLY_SHOW_LIFECYCLE=1; shift ;;
    --bucket-min) HOURLY_BUCKET_MIN="$2"; shift 2 ;;
    --chunk-size) HOURLY_CHUNK_SIZE="$2"; shift 2 ;;
    --date) HOURLY_DATE="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# //; s/^#$//'
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Refuse to clobber real boards.
if [[ "$PORT" == "7891" || "$PORT" == "7892" ]]; then
  echo "✗ refusing to use port $PORT (live QM/WB board). Pick another." >&2
  exit 2
fi

# Default sim dir = date-stamped scratch under ~/Desktop.
if [[ -z "$SIM_DIR" ]]; then
  SIM_DIR="${HOME}/Desktop/board-sim-$(date +%y%m%d)"
fi

# This script lives in dev/; the runtime scripts (serve.py) are in ../scripts.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/../scripts" && pwd )"
SERVE_PY="${SCRIPT_DIR}/serve.py"
if [[ ! -f "$SERVE_PY" ]]; then
  echo "✗ serve.py not found at $SERVE_PY" >&2
  exit 1
fi

PROJECT_ABS="$( cd "$PROJECT" && pwd )"
if [[ ! -d "$PROJECT_ABS" ]]; then
  echo "✗ project dir not found: $PROJECT" >&2
  exit 1
fi

echo "── board-steward simulate_install ────────────────────────────────"
echo "  project      $PROJECT_ABS"
echo "  sim-dir      $SIM_DIR"
echo "  port         $PORT"
echo "  discover     last ${DAYS}d, max ${MAX} sessions → cards"
echo "──────────────────────────────────────────────────────────────────"

# ---- teardown any prior sim on this port -------------------------------------
if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
  echo "↻ killing existing process on :$PORT"
  lsof -ti tcp:"$PORT" | xargs kill -9 2>/dev/null || true
  sleep 0.3
fi

# ---- wipe + recreate sim dir -------------------------------------------------
if [[ -d "$SIM_DIR" ]]; then
  echo "↻ wiping existing sim dir $SIM_DIR"
  rm -rf "$SIM_DIR"
fi
mkdir -p "$SIM_DIR"

# ---- spawn server = THE REAL INSTALL ENGINE ----------------------------------
# The sim runs the EXACT serve.py --bootstrap a real user triggers, so the
# simulation == the install experience. The only isolation: the board lives in
# the throwaway $SIM_DIR while history is mined from the real $PROJECT_ABS via
# --harvest-project (decoupled board-location vs harvest-source). serve.py's
# own daemon thread does the two-tier hourly fill (tier-1 last 1d fast →
# tier-2 older backfill) — identical to install.
if [[ -n "${HOURLY_DATE}" ]]; then
  echo "  (note: --date is ignored in sim==install mode; install always does the two-tier window)" >&2
fi

# Map the sim's --replay-mode to serve.py's --bootstrap-mode (sim == install).
# haiku is the only engine now; inline/discover are retired → everything routes to haiku.
case "$REPLAY_MODE" in
  haiku|hourly|"") BOOTSTRAP_MODE="haiku" ;;
  *) echo "  ! --replay-mode '${REPLAY_MODE}' retired (inline/discover removed) — using haiku" >&2; BOOTSTRAP_MODE="haiku" ;;
esac

SERVE_ARGS=(--bootstrap
            --project "$SIM_DIR"
            --harvest-project "$PROJECT_ABS"
            --port "$PORT"
            --profile "$PROFILE"
            --title "WorkBoard — $(basename "$PROJECT_ABS") (sim)"
            --bootstrap-mode "$BOOTSTRAP_MODE"
            --discover-days "$DAYS"
            --discover-max "$MAX"
            --bucket-min "$HOURLY_BUCKET_MIN"
            --chunk-size "$HOURLY_CHUNK_SIZE")
[[ "$LEGACY_DISCOVER" == "1" ]] && SERVE_ARGS+=(--legacy-discover)

LOG_FILE="${SIM_DIR}/serve.log"
echo "▶ spawning serve.py --bootstrap --bootstrap-mode $BOOTSTRAP_MODE (harvest: $PROJECT_ABS, last ${DAYS}d)"
nohup python3 "$SERVE_PY" "${SERVE_ARGS[@]}" > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "  pid=$SERVER_PID  log=$LOG_FILE"

# ---- wait for health ---------------------------------------------------------
for i in {1..30}; do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
if ! curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "✗ server didn't come up on :$PORT — check $LOG_FILE" >&2
  exit 1
fi

# Open the browser NOW so the user watches serve.py's bootstrap thread fly cards
# in live (tier-1 lands within seconds; tier-2 backfills in the background).
if [[ "$OPEN_BROWSER" == "1" ]]; then
  if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:${PORT}/"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:${PORT}/"
  fi
  OPEN_BROWSER=0   # don't reopen at end
fi

# ---- final report ------------------------------------------------------------
HEALTH=$(curl -s "http://127.0.0.1:${PORT}/health")
echo ""
echo "✓ sim board live"
echo "  URL    http://127.0.0.1:${PORT}/"
echo "  health $HEALTH"
echo "  log    $LOG_FILE"
echo "  stop   kill $SERVER_PID   (or: lsof -ti tcp:$PORT | xargs kill)"
echo ""

if [[ "$OPEN_BROWSER" == "1" ]]; then
  if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:${PORT}/"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:${PORT}/"
  fi
fi
