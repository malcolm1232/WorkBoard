#!/usr/bin/env python3
"""Fixture test for #566 subtask 1 — rev-baseline windowing in the Stop-hook
reconciliation backstop (_hook_stop_recon.py).

THE BUG (pre-fix): the backstop credited ANY rev advance since the last sign-off
as "this session carded its work" (carded = cur_rev > prev_rev OR ...). A
BETWEEN-SESSION background reconcile bumps board.rev via autonomous 'harvest'
moves — so the next session, even if it did substantive UN-carded work, was
wrongly judged "carded" and the miss went unflagged (#560 audit missed-gap edge).

THE FIX: credit a rev advance only when attributable to a GENUINE interactive
card action — a history event since the baseline whose via ∉ {harvest, autoship}.

This drives the real hook end-to-end (subprocess + crafted stdin payload + board
+ state file + transcript), since a bootstrap never fires the Stop hook. Three
scenarios:
  A  un-carded work + background-reconcile-only rev bump  → MUST flag (block)
  B  genuine interactive carding (via=agent)              → MUST NOT flag
  C  read-only session + background rev bump              → MUST NOT block
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Override with BOARD_STOP_HOOK=<path> to test a different code state (e.g. the
# pre-fix worktree, to prove this test actually catches the bug).
HOOK = Path(os.environ.get("BOARD_STOP_HOOK")
            or Path(__file__).resolve().parent.parent / "scripts" / "_hook_stop_recon.py")


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def run_case(name, *, history_via, edits, ship, card_actions, expect_block):
    """Build an isolated board/state/transcript and run the Stop hook.
    history_via: the `via` of the one board mutation that happened AFTER the
    baseline (drives the rev-attribution). edits/ship/card_actions: what the
    transcript shows this session did."""
    td = Path(tempfile.mkdtemp(prefix=f"sub1-{name}-"))
    bdir = td / "board"; bdir.mkdir()
    now = datetime.now(timezone.utc)
    base_at = now - timedelta(hours=2)          # last sign-off (baseline)
    move_at = now - timedelta(minutes=10)       # the post-baseline mutation

    # Board: rev advanced (100 -> 105) since baseline. The card's most-recent
    # history event is tagged `history_via` and timestamped after the baseline.
    board = {
        "rev": 105, "nextNum": 5,
        "columns": ["task", "inprogress", "done", "backlog"],
        "cards": [{
            "num": 1, "id": "c-x", "title": "some card", "column": "done",
            "doneAt": _iso(move_at),
            "history": [
                {"from": None, "to": "task", "via": "agent", "at": _iso(base_at - timedelta(hours=1))},
                {"from": "task", "to": "done", "via": history_via, "at": _iso(move_at)},
            ],
        }],
    }
    (bdir / "board.json").write_text(json.dumps(board))
    # Prior baseline state: rev 100 at base_at (a DIFFERENT, earlier session).
    (bdir / ".stop_recon_state.json").write_text(
        json.dumps({"rev": 100, "at": _iso(base_at)}))

    # Transcript: one real user prompt, then this session's activity. Edits are
    # to a file INSIDE the project root (td) so they count (#78 scoping).
    target = str(td / "src.py")
    lines = [{"type": "user", "message": {"content": "do the thing"}}]
    asst_blocks = []
    for _ in range(edits):
        asst_blocks.append({"type": "tool_use", "name": "Edit",
                            "input": {"file_path": target}})
    if ship:
        asst_blocks.append({"type": "tool_use", "name": "Bash",
                            "input": {"command": "git commit -m x && git push"}})
    if card_actions:
        asst_blocks.append({"type": "tool_use", "name": "Bash",
                            "input": {"command": "python3 card.py add --column task --title y"}})
    lines.append({"type": "assistant", "message": {"content": asst_blocks}})
    tpath = td / "transcript.jsonl"
    tpath.write_text("\n".join(json.dumps(o) for o in lines) + "\n")

    payload = json.dumps({"cwd": str(td), "transcript_path": str(tpath),
                          "session_id": f"sess-{name}"})
    cp = subprocess.run([sys.executable, str(HOOK)], input=payload,
                        capture_output=True, text=True, timeout=15)
    out = (cp.stdout or "").strip()
    blocked = False
    if out:
        try:
            blocked = json.loads(out).get("decision") == "block"
        except Exception:
            blocked = False
    # Also inspect the deferred recon_pending.json for an uncarded reason.
    pend = bdir / "recon_pending.json"
    uncarded_reason = False
    if pend.exists():
        data = json.loads(pend.read_text())
        uncarded_reason = any("un-carded" in r or "NO card.py" in r
                              for r in data.get("reasons", []))

    flagged = blocked or uncarded_reason
    ok = flagged == expect_block
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: blocked={blocked} "
          f"uncarded_reason={uncarded_reason} (expected flag={expect_block})")
    return ok


def main():
    print("#566 subtask 1 — rev-baseline windowing fixture test")
    results = []
    # A: substantive un-carded work; the ONLY post-baseline board change was a
    #    background reconcile (via=harvest). The OLD code saw rev 100->105 and
    #    said "carded" → no flag (the bug). FIX must FLAG it.
    results.append(run_case("A_harvest_masks_uncarded", history_via="harvest",
                            edits=4, ship=True, card_actions=False,
                            expect_block=True))
    # B: genuine interactive carding (via=agent) since baseline. Must NOT flag.
    results.append(run_case("B_genuine_agent_carding", history_via="agent",
                            edits=4, ship=True, card_actions=False,
                            expect_block=False))
    # C: read-only session (no edits/ships) + background rev bump. Must NOT block.
    results.append(run_case("C_readonly_plus_harvest", history_via="harvest",
                            edits=0, ship=False, card_actions=False,
                            expect_block=False))
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
