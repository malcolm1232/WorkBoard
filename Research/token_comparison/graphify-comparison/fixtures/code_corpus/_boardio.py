"""Shared board write-safety helpers — Phase 3.5a (flock) + 3.5b (rolling backups).

Imported by BOTH writers so they cannot corrupt or lose each other's work:
  - card.py  : direct-write fallback (when no server owns the board)
  - serve.py : the server's POST /board.json write path

Why this exists (VISION: "data loss is unforgivable" at millions-of-installs scale):
  * Two processes (a running server + a `card.py` invoked from a shell that can't
    reach it) can both read rev=N and write rev=N+1, silently losing one update.
    `board_lock` serializes every committed write on a single lock file so the
    read-modify-write windows can't interleave across processes.
  * `os.replace` is atomic, but once a bad/empty board is committed there is no
    way back. `write_backup` snapshots every committed rev to <board>/.backups/
    and keeps the newest N, so `card.py recover` (3.5c) can roll back.

Pure stdlib, no third-party deps. Sibling-importable: when either script runs
as `python3 .../scripts/<x>.py`, its own dir is sys.path[0], so `import _boardio`
resolves. serve.py also explicitly inserts scripts/ onto sys.path.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

try:
    import fcntl  # POSIX
    _HAVE_FCNTL = True
    msvcrt = None
except ImportError:  # Windows
    _HAVE_FCNTL = False
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

BACKUP_KEEP = 10
LOCK_NAME = ".board.lock"
BACKUP_DIR = ".backups"


@contextlib.contextmanager
def board_lock(target, timeout: float = 5.0):
    """Exclusive cross-process lock keyed on ``<board_dir>/.board.lock``.

    Both writers lock the SAME path (derived from the board.json location) so a
    server write and a direct fallback write serialize instead of racing.

    Yields True if the lock was acquired, False if it timed out. #609 — the
    AGENT CLI write paths (card.py::main and card_state.atomic_save) now CHECK
    the yielded bool and refuse to write (fail loudly so the user re-runs) rather
    than proceeding unlocked, because a silent lost update is unacceptable. The
    server's own atomic_write (serve.py) stays best-effort on timeout: it is the
    board owner and runs under an in-process _write_lock, and it also serves the
    BROWSER's POSTs, which have no retry — failing there would silently drop a
    user edit, a worse outcome than the rare cross-process timeout. This helper
    just reports acquisition; the write/abort policy lives in each caller.
    """
    target = Path(target)
    lock_path = target.parent / LOCK_NAME
    f = None
    acquired = False
    try:
        f = open(lock_path, "a+")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break  # proceed unlocked rather than lose the write
                time.sleep(0.05)
        yield acquired
    finally:
        if f is not None:
            if acquired:
                try:
                    if _HAVE_FCNTL:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            f.close()


RECON_LOCK_NAME = ".recon.lock"


@contextlib.contextmanager
def recon_lock(board):
    """Non-blocking exclusive lock keyed on ``<board_dir>/.recon.lock``.

    Distinct from ``board_lock`` (which waits-then-proceeds so a write is never
    dropped). Reconcile is the opposite: a SECOND concurrent reconcile must NOT
    run — two passes racing is what made cards shuffle twice and emit conflicting
    "already up to date" / "N brought up to date" lines. So this yields True only
    if the lock was free, False if another reconcile already holds it (no wait).
    Callers that get False simply skip — the in-flight pass already covers it.
    """
    lock_path = Path(board).parent / RECON_LOCK_NAME
    f = None
    acquired = False
    try:
        f = open(lock_path, "a+")
        try:
            if _HAVE_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if f is not None:
            if acquired:
                try:
                    if _HAVE_FCNTL:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            f.close()


def write_backup(board_path, data: bytes, keep: int = BACKUP_KEEP) -> None:
    """Snapshot a just-committed board to ``<board_dir>/.backups/board-<rev>.json``
    and prune to the newest ``keep`` revs.

    Best-effort: any failure is swallowed so backups can never break the write
    path. ``data`` is the exact bytes that were written to board.json.
    """
    try:
        board_path = Path(board_path)
        rev = _extract_rev(data)
        bdir = board_path.parent / BACKUP_DIR
        bdir.mkdir(exist_ok=True)
        dest = bdir / f"board-{rev}.json"
        tmp = bdir / f".board-{rev}.json.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        _prune(bdir, keep)
    except Exception:
        pass


def list_backups(board_path):
    """Return existing backup snapshots, newest rev first: [(rev, Path), ...]."""
    bdir = Path(board_path).parent / BACKUP_DIR
    if not bdir.is_dir():
        return []
    snaps = [(_rev_of(p), p) for p in bdir.glob("board-*.json")]
    snaps = [(r, p) for r, p in snaps if r >= 0]
    return sorted(snaps, key=lambda rp: rp[0], reverse=True)


def _extract_rev(data: bytes) -> int:
    try:
        return int(json.loads(data).get("rev", 0))
    except Exception:
        return 0


def _rev_of(p: Path) -> int:
    try:
        return int(p.stem.split("-", 1)[1])
    except (ValueError, IndexError):
        return -1


def _prune(bdir: Path, keep: int) -> None:
    if keep <= 0:
        return
    snaps = sorted(bdir.glob("board-*.json"), key=_rev_of)
    for p in snaps[:-keep]:
        try:
            p.unlink()
        except OSError:
            pass
