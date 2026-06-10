#!/usr/bin/env bash
# bench_recon.sh — reconcile/extraction A/B benchmark harness (#566).
#
# Runs N_RUNS three-day bootstraps from a given code state (REPO_ROOT's
# scripts/hourly_extractor.py) on throwaway boards, then records each run's
# column distribution + reconcile move counts to a results JSONL so the 6 code
# states (PRE-576 / CURRENT / after each subtask 1-4) can be compared
# apples-to-apples over the SAME real history window.
#
# Faithful to the 6/9 PRE/POST A/B invocation:
#   unset CLAUDECODE; hourly_extractor.py --project <WorkBoard> --days N
#     --bucket-min 30 --chunk-size 2 --workers 8 --pace 0  (reconcile ON)
#
# Isolation: each run writes ONLY to its own /tmp throwaway board; the extractor
# mines real history read-only. The live board is never touched.
#
# Usage: bench_recon.sh LABEL REPO_ROOT [N_RUNS] [DAYS]
#   LABEL      short tag, e.g. pre576 / current / sub1 / sub2 / sub3 / sub4
#   REPO_ROOT  repo whose scripts/ runs (a git worktree, or the live repo)
set -u
LABEL="${1:?need LABEL}"
REPO_ROOT="${2:?need REPO_ROOT}"
N="${3:-3}"
DAYS="${4:-3}"
PROJECT="/Users/malco/Desktop/WorkBoard"
RESULTS_DIR="${PROJECT}/dev/bench_results"
WORKDIR="/tmp/bench_recon"
mkdir -p "$RESULTS_DIR" "$WORKDIR"
RESULTS="${RESULTS_DIR}/results.jsonl"

echo "BENCH_START label=$LABEL repo=$REPO_ROOT runs=$N days=$DAYS"
for i in $(seq 1 "$N"); do
  BOARD_DIR="${WORKDIR}/${LABEL}-run${i}"
  rm -rf "$BOARD_DIR"; mkdir -p "$BOARD_DIR"
  printf '%s' '{"rev":1,"nextNum":1,"cards":[],"columns":["task","inprogress","done","backlog","super-urgent","notes"]}' \
    > "${BOARD_DIR}/board.json"
  echo "  [$LABEL run $i/$N] extracting ${DAYS}d ..."
  ( cd "$REPO_ROOT" && unset CLAUDECODE && \
    python3 scripts/hourly_extractor.py \
      --project "$PROJECT" --board "${BOARD_DIR}/board.json" \
      --days "$DAYS" --bucket-min 30 --chunk-size 2 --workers 8 --pace 0 ) \
    > "${BOARD_DIR}/run.log" 2>&1
  python3 - "$LABEL" "$i" "${BOARD_DIR}/board.json" "$RESULTS" <<'PY'
import json, sys, collections
label, run, boardp, results = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
try:
    d = json.load(open(boardp))
except Exception as e:
    d = {"cards": []}
cards = d.get("cards", [])
cc = collections.Counter(c.get("column") for c in cards)
def recon(to):
    return sum(1 for c in cards for h in (c.get("history") or [])
               if h.get("via") == "harvest" and h.get("to") == to)
rec = {
    "label": label, "run": run, "total": len(cards),
    "done": cc.get("done", 0), "backlog": cc.get("backlog", 0),
    "task": cc.get("task", 0), "inprogress": cc.get("inprogress", 0),
    "super": cc.get("super-urgent", 0), "notes": cc.get("notes", 0),
    "cols": dict(cc),
    "recon_backlog": recon("backlog"), "recon_done": recon("done"),
    "recon_super": recon("super-urgent"),
}
with open(results, "a") as f:
    f.write(json.dumps(rec) + "\n")
print(f"  [DONE {label} run {run}] {rec['total']} cards | "
      f"done={rec['done']} backlog={rec['backlog']} task={rec['task']} "
      f"ip={rec['inprogress']} | recon→backlog={rec['recon_backlog']} "
      f"recon→done={rec['recon_done']}")
PY
done
echo "BENCH_COMPLETE label=$LABEL"
