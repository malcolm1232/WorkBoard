#!/usr/bin/env python3
"""#627 regression: a tier-2 bucket whose extraction HARD-FAILS must NOT be
silently dropped + marked replay-complete as a clean fill.

Run:  python3 dev/test_627_silent_drop.py   →  exit 0 = all green, 1 = any fail.

Fault-injection, LLM-free and live-board-free: we monkeypatch the extraction
seam (`extract_cards_for_chunk`) and the board-writing helpers, then assert the
#627 invariants directly:

  L1  extract_cards_for_chunk RAISES on a genuine failure (subprocess non-zero)
      but RETURNS [] on a legitimately empty digest (failure ≠ empty).
  L2  _extract_haiku records a permanently-failing bucket in `failed_buckets`
      (it is NOT counted as 0-cards / silently dropped), and its recovery pass
      RECOVERS a transiently-failing bucket.
  L3  _mark_replay_complete stamps `partial: true` + `failed_buckets` when a
      drop occurred, yet still reopens the gate (_replay_complete → True, #384).

Pre-#627 there was no `failed_buckets` channel at all (the windows returned a
bare int and a failed bucket was indistinguishable from an empty one), so L2/L3
encode exactly the behavior that fixes the silent drop.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import hourly_extractor as H  # noqa: E402

_fails = 0


def check(cond: bool, msg: str) -> None:
    global _fails
    mark = "✓" if cond else "✗"
    print(f"  {mark} {msg}")
    if not cond:
        _fails += 1


# ── L1: failure RAISES, empty digest RETURNS [] ──────────────────────────────
def test_failure_vs_empty():
    print("L1: extract_cards_for_chunk distinguishes failure from empty")
    proj = Path("/tmp/wb627-proj")

    # (a) genuine failure: non-zero exit → ChunkExtractionError (not []).
    orig_digest, orig_run = H.build_digest, H.subprocess.run
    H.build_digest = lambda *a, **k: "some work happened"
    H.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
    try:
        raised = False
        try:
            H.extract_cards_for_chunk([("10:00", [{"x": 1}])], proj)
        except H.ChunkExtractionError:
            raised = True
        check(raised, "non-zero claude exit raises ChunkExtractionError")

        # (b) legitimately empty bucket: empty digest → [] (no raise).
        H.build_digest = lambda *a, **k: ""
        try:
            out = H.extract_cards_for_chunk([("11:00", [{"x": 1}])], proj)
            check(out == [], "empty digest returns [] (not a failure)")
        except H.ChunkExtractionError:
            check(False, "empty digest must NOT raise")
    finally:
        H.build_digest, H.subprocess.run = orig_digest, orig_run


# ── L2: _extract_haiku records perma-fail + recovers transient-fail ──────────
def _ev(fail=False):
    """A synthetic event with a real ts (sub-bucketing reads ev['ts'])."""
    e = {"ts": datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)}
    if fail:
        e["FAIL"] = True
    return e


def _run_haiku(buckets, chunks, patched_extract):
    """Drive _extract_haiku with board I/O stubbed out; return its result."""
    saved = {n: getattr(H, n) for n in
             ("extract_cards_for_chunk", "emit_card", "_banner_create",
              "_banner_update", "_save_snapshot", "reconcile_sweep")}
    emitted = []

    def fake_emit(card_py, board, card, *a, **k):
        emitted.append(card)
        return len(emitted)   # truthy card num

    H.extract_cards_for_chunk = patched_extract
    H.emit_card = fake_emit
    H._banner_create = lambda *a, **k: None
    H._banner_update = lambda *a, **k: None
    H._save_snapshot = lambda *a, **k: None
    H.reconcile_sweep = lambda *a, **k: 0
    try:
        n_cards, failed = H._extract_haiku(
            Path("/tmp/wb627-proj"), Path("/tmp/wb627/board.json"),
            Path("card.py"), buckets, chunks, sorted(buckets), [],
            bucket_min=60, workers=4, chunk_size=1, days=2, date_filter=None,
            show_lifecycle=False, pace_s=0.0, reconcile=False, phase="speedup",
            will_reconcile=True)
    finally:
        for n, v in saved.items():
            setattr(H, n, v)
    return n_cards, failed, emitted


def _is_bad(chunk):
    return any(ev.get("FAIL") for _, evs in chunk for ev in evs)


def test_perma_fail_recorded():
    print("L2a: a permanently-failing bucket is recorded, not silently dropped")
    # 4 single-bucket chunks; bucket 102's events carry a FAIL marker so EVERY
    # attempt (incl. sub-bucket retries + the recovery pass) keeps failing.
    buckets = {100: [_ev()], 101: [_ev()], 102: [_ev(fail=True)], 103: [_ev()]}
    chunks = [[100], [101], [102], [103]]

    def patched(chunk, project, timeout_s=90):
        if _is_bad(chunk):
            raise H.ChunkExtractionError(chunk[0][0])
        return [{"title": f"card {chunk[0][0]}"}]

    n_cards, failed, emitted = _run_haiku(buckets, chunks, patched)
    check(102 in failed, "bucket 102 recorded in failed_buckets")
    check(len(failed) == 1, f"exactly 1 bucket failed (got {failed})")
    check(n_cards == 3, f"3 good buckets emitted, failed one not counted (got {n_cards})")
    check(len(emitted) == 3, "no phantom card emitted for the failed bucket")


def test_transient_fail_recovered():
    print("L2b: a transiently-failing bucket is recovered by the retry pass")
    buckets = {100: [_ev()], 101: [_ev()], 102: [_ev(fail=True)], 103: [_ev()]}
    chunks = [[100], [101], [102], [103]]
    bad60 = H._bucket_label(102, 60)
    state = {"top": 0}   # top-level attempts on the original 60-min bad bucket

    def patched(chunk, project, timeout_s=90):
        if not _is_bad(chunk):
            return [{"title": f"card {chunk[0][0]}"}]
        label = chunk[0][0]
        if label == bad60:               # a TOP-LEVEL attempt on bucket 102
            state["top"] += 1
            if state["top"] >= 2:        # 1st = main pass (fail); 2nd = recovery (pass)
                return [{"title": "recovered 102"}]
        raise H.ChunkExtractionError(label)   # main-pass top-level + all sub-buckets

    n_cards, failed, emitted = _run_haiku(buckets, chunks, patched)
    check(failed == [], f"recovery pass cleared all failures (got {failed})")
    check(n_cards == 4, f"all 4 buckets emitted after recovery (got {n_cards})")


# ── L3: the gate is stamped partial yet still reopens ────────────────────────
def test_gate_partial_stamp():
    print("L3: replay gate records partial-failure yet reopens (#384 preserved)")
    with tempfile.TemporaryDirectory() as d:
        board = Path(d) / "board.json"
        board.write_text("{}")

        H._mark_replay_started(board, 2)
        check(H._replay_complete(board) is False,
              "gate CLOSED while replay in progress")

        # Partial fill: a bucket was dropped.
        H._mark_replay_complete(board, failed_buckets=[102])
        st = json.loads(H._replay_state_path(board).read_text())
        check(st.get("completed_card_replay") == 1, "gate flag flipped to 1")
        check(st.get("partial") is True, "stamped partial: true")
        check(st.get("failed_buckets") == [102], "recorded the dropped bucket")
        check(H._replay_complete(board) is True,
              "gate REOPENED despite partial (recon not stuck — #384)")

        # Clean fill: no failures.
        H._mark_replay_complete(board, failed_buckets=[])
        st = json.loads(H._replay_state_path(board).read_text())
        check(st.get("partial") is False, "clean fill stamps partial: false")
        check(H._replay_complete(board) is True, "gate open on clean fill")


# ── L4 (#642): a failed completion-write must fail OPEN, never stuck-closed ───
def test_gate_write_failure_fails_open():
    print("L4 (#642): failed gate-write fails OPEN, never permanently stuck")
    with tempfile.TemporaryDirectory() as d:
        board = Path(d) / "board.json"
        board.write_text("{}")
        H._mark_replay_started(board, 2)   # gate now CLOSED (flag 0 on disk)
        check(H._replay_complete(board) is False, "precondition: gate closed")

        # Simulate a write failure during completion (disk full / perms).
        orig = H._write_replay_state
        H._write_replay_state = lambda *a, **k: False
        try:
            H._mark_replay_complete(board, failed_buckets=[])
        finally:
            H._write_replay_state = orig
        # The stale closed-state file must be GONE → gate defaults open.
        check(not H._replay_state_path(board).exists(),
              "stale closed state removed when completion-write fails")
        check(H._replay_complete(board) is True,
              "gate FAILS OPEN — recon not permanently stuck (#642)")

        # And once the disk recovers, a normal completion still works.
        H._mark_replay_started(board, 2)
        H._mark_replay_complete(board, failed_buckets=[])
        st = json.loads(H._replay_state_path(board).read_text())
        check(st.get("completed_card_replay") == 1, "recovery: clean complete writes 1")


if __name__ == "__main__":
    test_failure_vs_empty()
    test_perma_fail_recorded()
    test_transient_fail_recovered()
    test_gate_partial_stamp()
    test_gate_write_failure_fails_open()
    print()
    if _fails:
        print(f"✗ {_fails} check(s) FAILED")
        sys.exit(1)
    print("✓ all #627 checks passed")
