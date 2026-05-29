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
REPLAY_MODE="hourly"                # "hourly" = LLM-per-bucket fly (canonical demo, #264) · "bulk" = no-API discover fallback
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

# Resolve script location → repo root → serve.py path.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
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

# ---- spawn server ------------------------------------------------------------
LOG_FILE="${SIM_DIR}/serve.log"
echo "▶ spawning serve.py --bootstrap"
nohup python3 "$SERVE_PY" \
  --bootstrap \
  --no-discover \
  --project "$SIM_DIR" \
  --port "$PORT" \
  --profile "$PROFILE" \
  --title "WorkBoard — $(basename "$PROJECT_ABS") (sim)" \
  > "$LOG_FILE" 2>&1 &
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

# Open the browser NOW (before discovery / replay) so the user watches cards
# pop in live. Final report still prints at the end.
if [[ "$OPEN_BROWSER" == "1" ]]; then
  if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:${PORT}/"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:${PORT}/"
  fi
  OPEN_BROWSER=0   # don't reopen at end
fi

# The bootstrap discovery thread runs in the background and streams cards via
# SSE; cards may still be arriving when the browser opens (that's the point —
# user sees them pop in live). Discovery streams real session history from the
# REAL project at $PROJECT_ABS, but writes cards into the SIM board.
#
# Note: serve.py --project is what controls where discovery walks for cwd
# matches. It also controls where the board lives. Same arg, two roles. So
# for a "fresh install on dir X" sim we point --project at the SIM dir (above)
# — discover.py walks ~/.claude/projects/*/sessions/*.jsonl and includes
# sessions whose cwd is at/under the sim dir, which is empty, so no cards
# stream. For "first-user feel on REAL history of project X" we'd swap
# --project to point at X — but then the sim's board.json would live INSIDE
# X, polluting it. The clean compromise: this script kicks off discovery
# manually pointed at the real project, via a one-shot subprocess invocation
# of card.py (mirroring what _stream_discovered_cards does in serve.py).

export SIM_BOARD_DIR="${SIM_DIR}/board"

# ---- hourly LLM-digest branch --------------------------------------------------
if [[ "$REPLAY_MODE" == "hourly" ]]; then
  echo "▶ hourly LLM extraction from $PROJECT_ABS history"
  HOURLY_ARGS=(--project "$PROJECT_ABS"
               --board   "${SIM_DIR}/board/board.json"
               --port    "$PORT"
               --days    "$DAYS"
               --bucket-min "$HOURLY_BUCKET_MIN"
               --chunk-size "$HOURLY_CHUNK_SIZE"
               --recent-first
               --max-buckets "$HOURLY_MAX_BUCKETS")
  if [[ -n "$HOURLY_DATE" ]]; then
    HOURLY_ARGS+=(--date "$HOURLY_DATE")
  fi
  if [[ "$HOURLY_SHOW_LIFECYCLE" == "1" ]]; then
    HOURLY_ARGS+=(--show-lifecycle)
  fi
  python3 "${SCRIPT_DIR}/hourly_extractor.py" "${HOURLY_ARGS[@]}"
  HOURLY_DONE=1   # share the gate so bulk-discover is skipped
fi

if [[ -z "${HOURLY_DONE:-}" ]]; then
echo "▶ discovering real cards from $PROJECT_ABS history"
python3 - "$PROJECT_ABS" "$PORT" "$DAYS" "$MAX" "${SCRIPT_DIR}" "${LEGACY_DISCOVER}" <<'PYEOF'
import sys, subprocess, json, time, urllib.request, urllib.parse
project, port, days, mx, sdir, legacy = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
)
# Run discover2.py (or legacy discover.py) against the REAL project.
script = f"{sdir}/discover.py" if legacy == "1" else f"{sdir}/discover2.py"
cap_flag = "--max-sessions" if legacy == "1" else "--max-tasks"
proc = subprocess.run(
    ["python3", script, "--project", project,
     "--days", days, cap_flag, mx],
    capture_output=True, text=True
)
if proc.returncode != 0:
    print(f"{script} failed: {proc.stderr[:400]}", file=sys.stderr)
    sys.exit(0)  # don't break the script
try:
    payload = json.loads(proc.stdout)
except json.JSONDecodeError:
    print(f"{script} returned non-JSON: {proc.stdout[:200]}", file=sys.stderr)
    sys.exit(0)
if legacy == "1":
    items = payload.get("sessions", [])
    items.sort(key=lambda s: s.get("startedAt", ""))
    label = "session"
else:
    items = payload.get("tasks", [])
    items.sort(key=lambda t: t.get("ts_start", ""))
    label = "task"
print(f"  → {len(items)} {label}(s) to stream into sim board")
import os
env = os.environ.copy()
env["BOARD_SERVER"] = f"http://127.0.0.1:{port}"
sys.path.insert(0, sdir)
from serve import _session_to_card_args, _task_to_card_args
mapper = _session_to_card_args if legacy == "1" else _task_to_card_args
for sess in items:
    args = mapper(sess)
    if not args:
        continue
    sim_board_json = os.path.join(os.environ.get("SIM_BOARD_DIR", ""), "board.json")
    # Pull out --column FINAL so we can override to task and walk the path.
    final_col = "backlog"
    try:
        ci = args.index("--column")
        final_col = args[ci + 1]
        args = args[:ci] + args[ci + 2:]
    except ValueError:
        pass
    # Born in task — every card pops here, then flies to its real home so
    # the user watches the chronological history reconstruct itself.
    try:
        subprocess.run(
            ["python3", f"{sdir}/card.py", "--board", sim_board_json, "add",
             "--column", "task"] + args,
            env=env, capture_output=True, text=True, timeout=8
        )
    except Exception as e:
        print(f"  ! card add failed: {e}", file=sys.stderr)
        continue
    # Resolve the num we just added so subsequent flies target the right card.
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/board.json", timeout=4) as r:
            board = json.loads(r.read())
        num = max((c.get("num", 0) for c in board["cards"]), default=0)
    except Exception:
        num = 0
    if num == 0:
        continue

    # Path table — every hop = one card.py fly call with its own pause.
    # final 'task' = 0 hops (just the pop-in).
    hops = []                                        # list of (col, flags_dict)
    if final_col == "backlog":
        hops = [("backlog", {})]
    elif final_col == "inprogress":
        hops = [("inprogress", {})]
    elif final_col == "blocked":
        hops = [("blocked", {})]
    elif final_col == "done":
        hops = [("inprogress", {}), ("done", {"writeup": "shipped (discovered)"})]
        # Bug bounces — sessions with bug signals re-open + re-ship to mirror
        # real life. Cap at 2 so wall-time stays sane.
        # discover.py used 'bugHints'; discover2.py uses 'bug_hits'.
        bugs = (sess.get("bug_hits") or sess.get("bugHints") or [])[:2]
        for bh in bugs:
            reason = (bh.splitlines()[0] if isinstance(bh, str) else "")[:80]
            hops.append(("inprogress", {"bug": reason or "regression"}))
            hops.append(("done", {"writeup": "patched"}))
    elif final_col == "task":
        hops = []
    else:
        hops = [(final_col, {})]                     # unknown col — just hop once

    for col, flags in hops:
        fly_args = ["python3", f"{sdir}/card.py", "--board", sim_board_json,
                    "fly", str(num), col]
        for k, v in flags.items():
            fly_args.extend([f"--{k}", v])
        try:
            subprocess.run(fly_args, env=env, capture_output=True, text=True, timeout=8)
        except Exception as e:
            print(f"  ! fly {num} → {col} failed: {e}", file=sys.stderr)
            break
    # fly --pause-ms (default 400) already paced each hop; small pause
    # between cards so the next pop-in doesn't overlap the last landing.
    time.sleep(0.15)
PYEOF
fi   # end bulk-discover gate (skipped in hourly mode)

# ---- lifecycle flight replay -------------------------------------------------
# Adds one fresh card and walks it task → inprogress → done with sleeps so the
# browser plays simulateUserDragMove (pickup-ghost-flight, e9e643e on 5/27).
# Static bulk discovery only triggers card-pop-in, not the cross-column glide.
if [[ "$LIFECYCLE" == "1" ]]; then
  echo "▶ lifecycle replay (interval=${LIFECYCLE_INTERVAL}s)"
  BOARD_JSON="${SIM_DIR}/board/board.json"
  BS_ENV="BOARD_SERVER=http://127.0.0.1:${PORT}"
  CARD_PY="${SCRIPT_DIR}/card.py"
  TS=$(date +%H%M%S)
  TITLE="SIMULATION lifecycle replay ${TS}"

  env "$BS_ENV" python3 "$CARD_PY" --board "$BOARD_JSON" add \
    --title "$TITLE" --column task --priority mid \
    --tag simulation --tag lifecycle \
    --origin "Generated by simulate_install.sh --lifecycle to demo the task→inprogress→done flight animation." \
    >/dev/null

  # Find the num we just assigned (highest num on board).
  NUM=$(curl -s "http://127.0.0.1:${PORT}/board.json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(max(c.get('num',0) for c in d['cards']))
")
  echo "  + #${NUM} → task"
  sleep "$LIFECYCLE_INTERVAL"

  env "$BS_ENV" python3 "$CARD_PY" --board "$BOARD_JSON" move "$NUM" inprogress >/dev/null
  echo "  ↻ #${NUM} task → inprogress  (watch the ghost-flight)"
  sleep "$LIFECYCLE_INTERVAL"

  env "$BS_ENV" python3 "$CARD_PY" --board "$BOARD_JSON" subtask add "$NUM" "explore options" >/dev/null
  env "$BS_ENV" python3 "$CARD_PY" --board "$BOARD_JSON" subtask add "$NUM" "ship it" >/dev/null
  echo "  + #${NUM} subtasks (active-work pulse)"
  sleep "$LIFECYCLE_INTERVAL"

  env "$BS_ENV" python3 "$CARD_PY" --board "$BOARD_JSON" move "$NUM" done \
    --writeup "Demo card from simulate_install.sh lifecycle replay. Walked task → inprogress → done at ${LIFECYCLE_INTERVAL}s intervals to exercise simulateUserDragMove." >/dev/null
  echo "  ✓ #${NUM} inprogress → done"
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
