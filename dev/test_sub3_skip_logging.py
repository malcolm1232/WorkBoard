#!/usr/bin/env python3
"""Fixture test for #566 subtask 3 — reconcile mover skip-logging
(hourly_reconcile.reconcile_sweep).

THE GAP (pre-fix): when the Haiku reconcile returned a move for a card # that
doesn't exist (hallucinated/stale) or a target column it invented, the mover
`continue`d with NO trace — the move silently vanished from the count.

THE FIX: every dropped move is logged to stderr (SKIP …) with the reason, plus a
'N moved, M skipped' summary.

Mocks the LLM call (no Haiku) so the move set is deterministic: 1 valid + 1
hallucinated num + 1 invented column. Set BOARD_SCRIPTS_DIR to test another tree.
"""
import json, os, sys, io, tempfile, importlib, contextlib
from pathlib import Path

os.environ.pop("CLAUDECODE", None)        # force the autonomous (mover) path
SCRIPTS = Path(os.environ.get("BOARD_SCRIPTS_DIR")
               or Path(__file__).resolve().parent.parent / "scripts")
sys.path.insert(0, str(SCRIPTS))
r = importlib.import_module("hourly_reconcile")


def main():
    print(f"#566 subtask 3 — reconcile skip-logging test (scripts={SCRIPTS})")
    td = Path(tempfile.mkdtemp(prefix="sub3-"))
    bdir = td / "board"; bdir.mkdir()
    board = bdir / "board.json"
    board.write_text(json.dumps({
        "rev": 1, "nextNum": 3,
        "columns": ["task", "backlog", "inprogress", "done", "super-urgent"],
        "cards": [
            {"num": 1, "id": "c-1", "title": "real card one", "column": "task",
             "tags": [], "history": []},
            {"num": 2, "id": "c-2", "title": "real card two", "column": "task",
             "tags": [], "history": []},
        ],
    }))
    card_py = SCRIPTS / "card.py"

    # Deterministic stand-in for the Haiku sweep: valid + hallucinated + invented.
    r._llm_reconcile = lambda cards, events, *a, **k: [
        {"num": 1, "target": "done", "reason": "shipped it"},
        {"num": 999, "target": "done", "reason": "ghost card"},
        {"num": 2, "target": "frobnicate", "reason": "invented column"},
    ]

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        moved = r.reconcile_sweep(card_py, board, [], only_discovered=False)
    err = buf.getvalue()
    print("--- captured stderr ---")
    print("\n".join("    " + l for l in err.splitlines() if "recon:" in l))

    checks = {
        "hallucinated #999 → SKIP logged": "SKIP #999" in err,
        "invented column 'frobnicate' → SKIP logged":
            "SKIP malformed move" in err and "frobnicate" in err,
        "skip summary shows '2 skipped'": "2 skipped" in err,
        "the 1 valid move still applied (returned 1)": moved == 1,
    }
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    ok = all(checks.values())
    print(f"\n{sum(checks.values())}/{len(checks)} passed")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
