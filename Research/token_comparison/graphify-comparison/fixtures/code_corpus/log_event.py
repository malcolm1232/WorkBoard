#!/usr/bin/env python3
"""Append one telemetry event to ~/.board-steward/telemetry/events.jsonl.

Called by the Steward as the LAST action of every invocation (after signoff).
The event captures: trigger, board state, what was read/written, drift detected,
bookend compliance, and free-form pain notes. Aggregated later by report.py
to answer "where is the skill struggling?" without relying on memory.

Usage:
    echo '{"trigger":"session-start", ...}' | python3 log_event.py
    python3 log_event.py --event '{"trigger":"after-ship", ...}'

Event schema (see telemetry/README.md for full doc):
    ts             ISO timestamp (auto-filled if missing)
    trigger        session-start | after-ship | session-end | manual | trigger-keyword:<kw>
    project        path to the board dir (so events from multi-project use are separable)
    board_rev      int — board.json rev at the time
    board_cards    int — total cards
    reads          [str] — files actually read: "index" | "board" | "archive:<YYYY-MM>"
    writes         {cards_moved, cards_added, subtasks_changed, writeups_filled}
    drift_flagged  int — items surfaced as drift
    drift_applied  int — drift items the user told you to apply this turn
    bookends       {greeted: bool, signed_off: bool}
    est_tokens     int — estimated tokens consumed this invocation
                   (sum of bytes read from board files + card.py output) / 4.
                   Lets report.py flag bloat trends. See docs/TOKEN_BUDGET.md.
    issues         [str] — known issue tags (see below)
    notes          str — free-form pain / observation

Known issue tags (encode pain so report.py can count + rank):
    missed-greeting
    missed-signoff
    read-full-when-index-enough
    asked-permission-for-mandatory
    drift-not-detected
    writeup-incomplete
    trigger-keyword-missed
    schema-confusion
    hook-misfire
"""
import json, os, sys, datetime, argparse
from pathlib import Path

# #378 DE-SPRAWL: telemetry lives in the FIXED home dir (alongside the port
# registry in ~/.board-steward/), NOT under the old ~/.agents install path or
# the versioned plugin cache — so it survives plugin upgrades. BOARD_TELEMETRY_FILE
# overrides it (e2e/test isolation). report.py reads the SAME resolution.
EVENTS_FILE = Path(os.environ.get("BOARD_TELEMETRY_FILE")
                   or Path.home() / ".board-steward/telemetry/events.jsonl")


def write_event(event: dict) -> dict:
    """Append one telemetry event to EVENTS_FILE and return it (ts auto-filled).
    The SINGLE writer for the events file — CLI main() and in-process callers
    (card.py verb counting) both go through here so the path + schema live in
    one place. Caller is responsible for catching errors if it must not fail."""
    event.setdefault("ts", datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", help="event JSON on the command line (alternative to stdin)")
    args = ap.parse_args()

    raw = args.event if args.event else sys.stdin.read()
    if not raw.strip():
        print("no event payload (pass --event or pipe JSON via stdin)", file=sys.stderr)
        sys.exit(2)
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"bad JSON: {e}", file=sys.stderr)
        sys.exit(2)

    event = write_event(event)
    print(f"logged event ts={event['ts']} trigger={event.get('trigger','?')}")


if __name__ == "__main__":
    main()
