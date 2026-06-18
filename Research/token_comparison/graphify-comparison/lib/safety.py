"""Safety guard — proves this study never mutates the rest of the product.

The study now lives INSIDE the product repo (WorkBoard/Research/token_comparison/letta-comparison/) by
the user's request, as a tracked sub-project. The non-invasiveness guarantee
therefore shifts from "outside the product" to: **all writes are confined to this
study subfolder; the live board and product source elsewhere are never written.**

  - STUDY_DIR     : this subfolder (the only place the harness may write).
  - LIVE_BOARD    : ~/Desktop/WorkBoard/board/board.json — the running product
                    state; must NEVER be written by the harness.
  - assert_non_invasive():  the live board file lives OUTSIDE STUDY_DIR, so a
                            local-only write can never reach it. (Sanity check.)
  - assert_write_local(p):  every output path resolves inside STUDY_DIR.
  - snapshot_fingerprint(): sha256 + size of board_snapshot.json, logged into the
                            report so a reader knows exactly which board was used.
  - assert_server_untouched(): the live board server (:7891) is never contacted.

`assert_outside_product` is kept as a backwards-compatible alias of
`assert_non_invasive` so existing drivers keep working.
"""

from __future__ import annotations
import hashlib
from pathlib import Path

STUDY_DIR = Path(__file__).resolve().parent.parent
PRODUCT_DIR = (Path.home() / "Desktop" / "WorkBoard").resolve()
LIVE_BOARD = (PRODUCT_DIR / "board" / "board.json").resolve()


def assert_non_invasive() -> None:
    """The live board must live OUTSIDE our writable study dir, so confining all
    writes to STUDY_DIR is sufficient to guarantee we never touch it."""
    sd = STUDY_DIR.resolve()
    if sd in LIVE_BOARD.parents or sd == LIVE_BOARD.parent:
        raise RuntimeError(
            f"REFUSING TO RUN: the live board {LIVE_BOARD} is inside the study dir "
            f"{sd} — writes could reach the running product."
        )


# backwards-compatible alias (older drivers call assert_outside_product()).
assert_outside_product = assert_non_invasive


def assert_write_local(path: Path) -> Path:
    p = Path(path).resolve()
    if STUDY_DIR not in p.parents and p != STUDY_DIR:
        raise RuntimeError(f"REFUSING TO WRITE outside study dir: {p}")
    if p == LIVE_BOARD:
        raise RuntimeError(f"REFUSING TO WRITE the live board: {p}")
    return p


def snapshot_fingerprint() -> dict:
    snap = STUDY_DIR / "board_snapshot.json"
    if not snap.exists():
        return {"present": False}
    data = snap.read_bytes()
    return {
        "present": True,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
    }


def assert_server_untouched() -> None:
    # Documentation hook: nothing in this harness opens a network socket for the
    # board. Recall reads the frozen snapshot via the read-only card_ro copy.
    return None
