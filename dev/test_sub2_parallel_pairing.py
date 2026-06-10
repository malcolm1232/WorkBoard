#!/usr/bin/env python3
"""Fixture test for #566 subtask 2 — content-correlated subagent pairing
(_hook_subagent_recon.py).

THE BUG (pre-fix): SubagentStop popped the OLDEST queue entry (blind FIFO). Two
subagents launched A-then-B but finishing B-then-A would mis-pair: B's stop pops
A's entry → B's writeup lands on A's card.

THE FIX: correlate a stop to ITS spawn by the subagent's task-prompt signature
(its own first user message), popping the matching entry regardless of order.

This drives the real do_spawn/do_stop against a throwaway board in collab mode
(each subagent gets its own card), simulating OUT-OF-ORDER completion. Set
BOARD_SCRIPTS_DIR=<dir> to import a different code state (proves the bug on the
pre-fix tree).
"""
import json, os, sys, tempfile, importlib
from pathlib import Path

SCRIPTS = Path(os.environ.get("BOARD_SCRIPTS_DIR")
               or Path(__file__).resolve().parent.parent / "scripts")
sys.path.insert(0, str(SCRIPTS))
recon = importlib.import_module("_hook_subagent_recon")


def _board_card_by_title(bp, title_frag):
    d = json.loads(Path(bp).read_text())
    for c in d.get("cards", []):
        if title_frag.lower() in (c.get("title", "")).lower():
            return c
    return None


def _transcript(td, prompt):
    """A minimal subagent transcript whose first user message is `prompt`."""
    p = td / f"tr-{abs(hash(prompt))}.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "message": {"content": prompt}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done."}]}},
    ]) + "\n")
    return p


def main():
    print(f"#566 subtask 2 — parallel pairing test (scripts={SCRIPTS})")
    td = Path(tempfile.mkdtemp(prefix="sub2-"))
    bdir = td / "board"; bdir.mkdir()
    # collab mode → each subagent gets its OWN card (so we can check pairing).
    (bdir / "board.json").write_text(json.dumps({
        "rev": 1, "nextNum": 1, "settings": {"subagentCards": "collab"},
        "columns": ["task", "inprogress", "done", "backlog"], "cards": [],
    }))

    A_prompt = "Find correctness bugs in the alpha pricing module only"
    B_prompt = "Audit the beta telemetry pipeline for race conditions only"

    def spawn(desc, prompt):
        recon.do_spawn({"tool_name": "Agent", "cwd": str(td),
                        "tool_input": {"description": desc,
                                       "subagent_type": "general-purpose",
                                       "prompt": prompt}})
    # Launch A then B (FIFO queue order: A, B).
    spawn("Audit ALPHA module", A_prompt)
    spawn("Audit BETA module", B_prompt)

    cardA = _board_card_by_title(bdir / "board.json", "ALPHA")
    cardB = _board_card_by_title(bdir / "board.json", "BETA")
    assert cardA and cardB, "both subagent cards should exist after spawn"
    # (Note: the one-active-card guard leaves the 2nd card in 'task' — incidental;
    # subtask 2 is about WHICH card the stop's writeup pairs to, i.e. goes done.)

    # Finish B FIRST (out of order). Its stop carries B's transcript. Correlation
    # must move the BETA card to done — NOT the ALPHA card (which blind FIFO,
    # popping the oldest entry, would wrongly close).
    recon.do_stop({"cwd": str(td),
                   "transcript_path": str(_transcript(td, B_prompt))})
    a1 = _board_card_by_title(bdir / "board.json", "ALPHA")
    b1 = _board_card_by_title(bdir / "board.json", "BETA")
    ok_b = (b1["column"] == "done" and a1["column"] != "done")
    print(f"  [{'PASS' if ok_b else 'FAIL'}] B finishes first → "
          f"BETA={b1['column']} (want done), ALPHA={a1['column']} (want NOT done)")

    # Then finish A.
    recon.do_stop({"cwd": str(td),
                   "transcript_path": str(_transcript(td, A_prompt))})
    a2 = _board_card_by_title(bdir / "board.json", "ALPHA")
    b2 = _board_card_by_title(bdir / "board.json", "BETA")
    ok_a = (a2["column"] == "done" and b2["column"] == "done")
    print(f"  [{'PASS' if ok_a else 'FAIL'}] A finishes second → "
          f"ALPHA={a2['column']} (want done), BETA={b2['column']} (want done)")

    passed = ok_b and ok_a
    print(f"\n{'2/2 passed' if passed else 'FAILED'}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
