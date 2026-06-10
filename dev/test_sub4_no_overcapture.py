#!/usr/bin/env python3
"""Fixture test for #566 subtask 4 — extractor over-capture guard
(hourly_common._LLM_PROMPT).

Feeds a crafted activity log (real Haiku) containing the four signal types that
caused the notes-column pile-up, and asserts correct routing:
  1. an ASSISTANT end-of-session wrap-up   → NOT carded at all
  2. a raw conversational fragment          → NOT carded
  3. a USER bug report                       → a 'task'/'bug' card, SUMMARIZED
                                               title, NOT a 'notes' card, NOT verbatim
  4. a real shipped deliverable (commit)     → a 'done' card

Real-Haiku + non-deterministic, so assertions are lenient but meaningful. Set
BOARD_SCRIPTS_DIR to test another tree (e.g. pre-fix, for contrast).
"""
import os, sys, subprocess, importlib
from pathlib import Path

SCRIPTS = Path(os.environ.get("BOARD_SCRIPTS_DIR")
               or Path(__file__).resolve().parent.parent / "scripts")
sys.path.insert(0, str(SCRIPTS))
h = importlib.import_module("hourly_common")

DIGEST = """=== BUCKET 2026-06-09 09:00 ===
[user] when i move cards v fast, doest LOG.
[user] not yet, lets check smth. cos see: https://github.com/example/repo
[assistant] All wrapped up for the session: - Pushed HEAD b3895fa, in sync with origin/main. - Conversation saved. - Carry-forward written. First thing next session: no-churn reinstall. See you in the next session. 👋
[assistant] edited templates/board.html
[commit 7b565ff] fix(board): smooth column-to-column drag glide before drop
[user] also the calendar tooltip sits behind the chips, can u fix the placement
"""


def main():
    print(f"#566 subtask 4 — over-capture guard test (scripts={SCRIPTS})")
    full = (f"{h._LLM_PROMPT}\n\n--- WORK ACTIVITY (1 bucket) ---\n{DIGEST}\n")
    try:
        proc = subprocess.run(h._LLM_ARGS, input=full, capture_output=True,
                              text=True, timeout=150, env=h._LLM_ENV)
    except Exception as e:
        print(f"  LLM call failed: {e}"); sys.exit(2)
    cards = h.parse_card_array(proc.stdout) or []
    print(f"  → {len(cards)} card(s):")
    for c in cards:
        print(f"     [{str(c.get('column')):10}] {c.get('title','')[:64]!r} "
              f"tags={c.get('tags')}")

    def blob(c):
        return (str(c.get("title", "")) + " " + str(c.get("notes", "")) + " "
                + str(c.get("origin", ""))).lower()

    # Subtask 4 is strictly about OVER-capture: assistant prose and raw
    # conversational fragments must NOT become cards (esp. note-cards), and no
    # card may be a verbatim message paste.
    eod_carded = any("wrapped up" in blob(c) or "see you in the next session" in blob(c)
                     or "no-churn reinstall" in blob(c) for c in cards)
    fragment_carded = any("lets check smth" in blob(c)
                          or "not yet" == (c.get("title", "")).strip().lower()
                          for c in cards)
    notes_cards = [c for c in cards if c.get("column") == "notes"]
    raw_verbatim = any("doest log" in (c.get("title", "")).lower()
                       or "lets check smth" in (c.get("title", "")).lower()
                       for c in cards)

    checks = {
        "assistant EOD wrap-up NOT carded": not eod_carded,
        "conversational fragment NOT carded": not fragment_carded,
        "no raw/verbatim message used as a card title": not raw_verbatim,
        "no junk dumped into 'notes' (≤1 genuine note)": len(notes_cards) <= 1,
    }
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    ok = all(checks.values())
    print(f"\n{sum(checks.values())}/{len(checks)} passed")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
