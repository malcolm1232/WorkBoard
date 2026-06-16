#!/usr/bin/env python3
"""#638 regression: the BOARD-LOAD HUD must show LIVENESS during a slow/silent
stage (a grinding Haiku chunk or the 60-90s end-of-replay reconcile) so it never
freezes on a stale number and looks hung.

Run:  python3 dev/test_638_hud_heartbeat.py  →  exit 0 = green, 1 = a fail.

Tests the progress_heartbeat context manager directly with _emit_progress patched
to a recorder (no card.py subprocess, no live board). Short interval keeps it fast.

  H1  STALL: no .touch() for > interval → a 'still working… mm:ss' tick is emitted,
      carrying the live (done, total, base_label, phase) from status().
  H2  BUSY: frequent .touch() (normal per-chunk progress) → NO heartbeat tick fires.
  H3  LIVE COUNTS: status() read at tick time reflects updated counts.
  H4  CLEAN STOP: the daemon thread stops emitting after the with-block exits.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import hourly_emit as E  # noqa: E402

_fails = 0


def check(cond: bool, msg: str) -> None:
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        _fails += 1


class _Rec:
    """Capture _emit_progress calls in place of the card.py subprocess."""
    def __init__(self):
        self.calls = []

    def __call__(self, card_py, board, done, total, label="", phase="",
                 final=False):
        self.calls.append({"done": done, "total": total, "label": label,
                           "phase": phase, "final": final})


def _with_recorder(fn):
    rec = _Rec()
    saved = E._emit_progress
    E._emit_progress = rec
    try:
        fn(rec)
    finally:
        E._emit_progress = saved
    return rec


def test_stall_fires():
    print("H1: stall → heartbeat tick with live status + phase")
    def body(rec):
        with E.progress_heartbeat(Path("card.py"), Path("b.json"),
                                  lambda: (3, 10, "checking nothing's missed…"),
                                  phase="reconcile", interval=0.2):
            time.sleep(0.75)   # no touch → should tick at least once
    rec = _with_recorder(body)
    ticks = [c for c in rec.calls if "still working" in c["label"]]
    check(len(ticks) >= 1, f"emitted ≥1 heartbeat tick during stall (got {len(ticks)})")
    if ticks:
        t = ticks[0]
        check(t["done"] == 3 and t["total"] == 10, "tick carries live (done,total)")
        check(t["phase"] == "reconcile", "tick carries the phase")
        check("checking nothing's missed…" in t["label"], "tick keeps base label")


def test_busy_no_fire():
    print("H2: frequent touch (normal progress) → no heartbeat tick")
    def body(rec):
        with E.progress_heartbeat(Path("card.py"), Path("b.json"),
                                  lambda: (1, 5, "base"),
                                  phase="solo", interval=0.3) as pulse:
            for _ in range(6):
                pulse.touch()
                time.sleep(0.1)   # never quiet for a full interval
    rec = _with_recorder(body)
    ticks = [c for c in rec.calls if "still working" in c["label"]]
    check(len(ticks) == 0, f"no heartbeat while touched (got {len(ticks)})")


def test_live_counts():
    print("H3: status read at tick time reflects updated counts")
    state = {"done": 0}
    def body(rec):
        with E.progress_heartbeat(Path("card.py"), Path("b.json"),
                                  lambda: (state["done"], 4, "base"),
                                  phase="speedup", interval=0.2):
            time.sleep(0.35)
            state["done"] = 3      # advance, then let another tick capture it
            time.sleep(0.4)
    rec = _with_recorder(body)
    ticks = [c for c in rec.calls if "still working" in c["label"]]
    check(any(t["done"] == 3 for t in ticks),
          f"a tick reflects the updated count=3 (got {[t['done'] for t in ticks]})")


def test_clean_stop():
    print("H4: no ticks after the with-block exits")
    def body(rec):
        with E.progress_heartbeat(Path("card.py"), Path("b.json"),
                                  lambda: (0, 1, "base"), interval=0.2):
            time.sleep(0.3)
        rec.calls.clear()          # forget ticks emitted while active
        time.sleep(0.5)            # the thread must be stopped now
    rec = _with_recorder(body)
    check(len(rec.calls) == 0, f"no emits after exit (got {len(rec.calls)})")


if __name__ == "__main__":
    test_stall_fires()
    test_busy_no_fire()
    test_live_counts()
    test_clean_stop()
    print()
    if _fails:
        print(f"✗ {_fails} check(s) FAILED")
        sys.exit(1)
    print("✓ all #638 checks passed")
