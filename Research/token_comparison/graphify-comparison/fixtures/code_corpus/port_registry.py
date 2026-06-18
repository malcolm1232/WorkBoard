"""Shared port registry for board-steward.

A tiny JSON file at ~/.config/board-steward/port-registry.json maps each
running board's absolute path → {port, pid, started_at}. serve.py writes its
entry on boot; card.py + hooks consult it for O(1) port lookup instead of
probing 7891-7900 every CLI call.

The registry is advisory, not authoritative — readers MUST verify the entry
is alive (the /health-ping check is the consumer's job) before trusting it.
This keeps the file self-healing across crashes / SIGKILL / laptop sleep
without needing a daemon to scrub it.
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


REGISTRY_ENV = "BOARD_REGISTRY"
ASSIGN_ENV = "BOARD_ASSIGNMENTS"
ACTIVE_ENV = "BOARD_ACTIVE"

# Port window for auto-designation. 7891 onwards, lowest free first — 7891 is
# assumed least-used, and each project then claims the next gap.
PORT_LO = 7891
PORT_HI = 7999

# Public for tests / inspectors. Lives directly in $HOME (not ~/.config,
# which on macOS is often root-owned and unwritable for normal users).
DEFAULT_PATH = Path.home() / ".board-steward" / "port-registry.json"
# Sticky board→port DESIGNATION, separate from the liveness registry above.
# This survives the server dying (a project keeps its port across restarts);
# the registry tracks who's live right now, this tracks who OWNS which port.
ASSIGN_PATH = Path.home() / ".board-steward" / "port-assignments.json"
# The board the human last INTERACTED with (a card mutation, a serve, a
# bootstrap). Used by the SessionStart hook + card.py's find_board fallback to
# disambiguate when Claude opens at $HOME with no cwd board and several boards
# exist: reopen the one in active use, not whichever board.json happens to have
# the newest file mtime (#mb — mtime picked the wrong board when two boards were
# edited the same session).
#
# #611 — this is now SESSION-AWARE to stop concurrent sessions clobbering each
# other's pointer. The file is JSON:
#   {"global":   {"board": <dir>, "at": <iso>},          # most-recent, any session
#    "sessions": {<session_id>: {"board": <dir>, "at": <iso>}, ...}}
# A reader passes its CLAUDE_CODE_SESSION_ID → resolves ITS OWN board (so session
# A at $HOME never lands on the board session B just touched, and a RESUMED
# session reopens its own board). A fresh/unseen session falls back to `global`
# (the previous single-pointer behaviour). The OLD single-line format is read
# transparently as `global.board`, so no migration step is needed. The sessions
# map is capped/pruned on write (see _prune_sessions) so it can't grow unbounded.
ACTIVE_PATH = Path.home() / ".board-steward" / "last-active"


def registry_path() -> Path:
    env = os.environ.get(REGISTRY_ENV)
    if env:
        return Path(env).expanduser()
    return DEFAULT_PATH


def assignments_path() -> Path:
    env = os.environ.get(ASSIGN_ENV)
    if env:
        return Path(env).expanduser()
    return ASSIGN_PATH


def active_path() -> Path:
    env = os.environ.get(ACTIVE_ENV)
    if env:
        return Path(env).expanduser()
    return ACTIVE_PATH


def _atomic_write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, p)


def read() -> dict:
    """Return the full registry dict, or {} if missing/corrupt."""
    p = registry_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists. Signal 0 probes without killing."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # EPERM means it exists but we can't signal it — still alive.
        return True
    return True


def prune(d: dict | None = None) -> dict:
    """Drop entries whose server PID is dead OR whose board dir no longer
    exists on disk. Returns the cleaned dict. This is the real self-heal the
    module's docstring promised: transient paths (e.g. /tmp demo runs) never
    reboot to stomp their own row, so without an active prune they accrete
    forever (#374 — registry hit 53 entries, 52 dead). Cheap: one os.kill(,0)
    + one stat per row."""
    if d is None:
        d = read()
    cleaned = {
        k: v for k, v in d.items()
        if isinstance(v, dict)
        and _pid_alive(v.get("pid", -1))
        and Path(k).exists()
    }
    return cleaned


def write(board_dir: str | os.PathLike, port: int, pid: int) -> None:
    """Register THIS process as serving `board_dir` on `port`. Idempotent.

    Prunes dead/stale rows first (self-heal), then stomps any prior entry for
    the same board path (the prior server is presumed dead)."""
    key = str(Path(board_dir).resolve())
    d = prune(read())
    d[key] = {
        "port": int(port),
        "pid": int(pid),
        "started_at": _now_iso(),
    }
    _atomic_write_json(registry_path(), d)


def remove(board_dir: str | os.PathLike) -> None:
    """Drop the entry for `board_dir`. Safe if missing."""
    key = str(Path(board_dir).resolve())
    d = read()
    if key in d:
        del d[key]
        _atomic_write_json(registry_path(), d)


def lookup(board_dir: str | os.PathLike) -> int | None:
    """Return the cached port for `board_dir`, or None if unregistered.

    Caller MUST verify the server is alive via /health before trusting it —
    this is just a hint, not a guarantee. Stale entries self-heal when the
    next serve.py for the same board path overrides."""
    key = str(Path(board_dir).resolve())
    return (read().get(key) or {}).get("port")


def assignments() -> dict:
    """Return the sticky board→port designation map, or {} if missing/corrupt."""
    p = assignments_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def assign(board_dir: str | os.PathLike, preferred: int | None = None,
           lo: int = PORT_LO, hi: int = PORT_HI) -> int:
    """Return the DESIGNATED port for `board_dir`, allocating one if needed.

    The designation is sticky and persisted: a project keeps the same port
    forever (across server restarts), so every consumer resolves it the same
    way and two projects can never collide (#374 — WorkBoard + QuantifyMe both
    defaulted to 7891). Allocation rule: reuse the existing designation if the
    board already has one; else take `preferred` if it's in-window and unclaimed;
    else the lowest free port from `lo` upward. GCs designations whose board dir
    no longer exists so the window doesn't fill with dead projects."""
    key = str(Path(board_dir).resolve())
    # #633 — the read→scan→write below is a TOCTOU race: two sessions first-touching
    # DIFFERENT new boards concurrently both read the map as empty and pick the SAME
    # lowest-free port → collision. Serialize the RMW on the assignments lock and
    # re-read INSIDE it so the port scan sees committed designations. Best-effort
    # (degrades to the old unlocked behaviour on lock timeout).
    with _file_lock(assignments_path().with_suffix(".lock")):
        a = {k: v for k, v in assignments().items() if Path(k).exists()}
        if key in a:
            port = int(a[key])
        else:
            taken = {int(v) for v in a.values()}
            if preferred is not None and lo <= preferred <= hi and preferred not in taken:
                port = int(preferred)
            else:
                port = next((p for p in range(lo, hi + 1) if p not in taken), lo)
            a[key] = port
        _atomic_write_json(assignments_path(), a)
    return port


def set_port(board_dir: str | os.PathLike, port: int) -> None:
    """Force `board_dir`'s designation to `port`, overwriting any prior one.
    Used after a bind to record the port that actually came up (e.g. when the
    preferred designation was held by a stray process and we walked forward)."""
    key = str(Path(board_dir).resolve())
    with _file_lock(assignments_path().with_suffix(".lock")):  # #633 — same RMW race as assign()
        a = {k: v for k, v in assignments().items() if Path(k).exists()}
        a[key] = int(port)
        _atomic_write_json(assignments_path(), a)


_ACTIVE_KEEP = 32  # cap on the per-session map (size, not age — never evict a live session)


@contextlib.contextmanager
def _file_lock(lock_path, timeout: float = 1.0):
    """Best-effort cross-process exclusive lock keyed on `lock_path`.

    Mirrors _boardio.board_lock's proven flock pattern but is inlined here so
    port_registry (imported by lightweight hook one-liners) needn't pull in
    _boardio's backup machinery. Used to serialize read→modify→write of the
    SHARED registry files where two writers would otherwise lose each other's
    entry — last-active sessions (#611) and port designations (#633 assign()
    TOCTOU). Yields True if acquired; on timeout yields False and the caller
    proceeds ANYWAY (degrading to lossy beats blocking or raising inside a
    never-raise write). Lock files are tiny and writes sub-ms, so contention is
    rare. Also re-usable cross-module (e.g. _hook_stop_recon's recon_pending
    merge) — pass any lock path."""
    lock_path = Path(lock_path)
    f = None
    acquired = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
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
                    break  # proceed unlocked rather than block the write
                time.sleep(0.02)
        yield acquired
    except OSError:
        yield acquired  # couldn't even open the lock file → proceed best-effort
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


def _active_lock(timeout: float = 1.0):
    """Back-compat thin wrapper: the last-active file's lock (#611)."""
    return _file_lock(active_path().with_suffix(".lock"), timeout)


def session_id() -> str:
    """Canonical identity of the calling session — the SINGLE source of truth for
    session-scoped routing (last-active) and pulses (activeWork).

    Lives here, in the owner of all cross-board state, so the WRITE side
    (card.py _mark_active → set_active) and the READ side (card_state.find_board →
    get_active) key by the EXACT same value. Splitting it (raw env on read, this
    on write) silently broke session routing for every automation path (#633
    review). A Bash tool call in a Claude Code session exposes
    CLAUDE_CODE_SESSION_ID; automation (recon/e2e/sim/replay — they set
    BOARD_SKIP_DECOMPOSE_CHECK) shares one synthetic '_auto' slot; a bare manual
    CLI run falls back to '_cli'."""
    if os.environ.get("BOARD_SKIP_DECOMPOSE_CHECK") == "1":
        return "_auto"
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or "_cli"


def _read_active() -> dict:
    """Parse the last-active file into the normalized {"global":..,"sessions":..}.

    Tolerant: the OLD single-line format (a bare board dir) reads as global.board
    so no migration is needed; corrupt/hand-edited/missing → an empty shell.
    Always returns a dict with both keys present."""
    empty = {"global": {}, "sessions": {}}
    try:
        p = active_path()
        if not p.exists():
            return empty
        raw = p.read_text().strip()
    except OSError:
        return empty
    if not raw:
        return empty
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Legacy single-line path (a bare dir never parses as JSON).
        return {"global": {"board": raw, "at": None}, "sessions": {}}
    if not isinstance(d, dict):
        return empty
    g = d.get("global") if isinstance(d.get("global"), dict) else {}
    s = d.get("sessions") if isinstance(d.get("sessions"), dict) else {}
    return {"global": g, "sessions": s}


def _prune_sessions(sessions: dict, keep: int = _ACTIVE_KEEP) -> dict:
    """Drop dead-on-disk session entries, then cap to the `keep` newest by `at`.

    Size cap, not a TTL — a long-running multi-day session must keep its own
    entry, so we never evict by age (only by being supplanted by newer sessions
    or by its board vanishing)."""
    live = {sid: e for sid, e in sessions.items()
            if isinstance(e, dict) and e.get("board") and Path(e["board"]).exists()}
    if len(live) <= keep:
        return live
    newest = sorted(live.items(), key=lambda kv: kv[1].get("at") or "", reverse=True)[:keep]
    return dict(newest)


def set_active(board_dir: str | os.PathLike, session_id: str | None = None) -> None:
    """Record `board_dir` as the board last interacted with (#611 session-aware).

    Always updates `global` (most-recent-wins, the fresh-session fallback) and,
    when `session_id` is given, that session's own entry — so concurrent sessions
    on different boards no longer clobber each other's pointer. Best-effort and
    never raises: a failed write here must never break the card mutation / serve
    / bootstrap that triggered it. The read-modify-write runs under a best-effort
    flock so distinct-session entries survive concurrent writers.

    NOTE: callers pass session_id() (this module) — the SAME identity the read
    side resolves by — which collapses a bare CLI run to "_cli" and all automation
    (recon/e2e/sim) to "_auto". Those are intentionally COARSE shared slots: two
    concurrent "_auto" runs can still clobber each other's slot, but real human
    sessions get distinct ids, which is the case #611 targets, and `global` is
    always written regardless."""
    try:
        key = str(Path(board_dir).resolve())
        now = _now_iso()
        with _active_lock():
            d = _read_active()
            d["global"] = {"board": key, "at": now}
            if session_id:
                d["sessions"][session_id] = {"board": key, "at": now}
            d["sessions"] = _prune_sessions(d["sessions"])
            _atomic_write_json(active_path(), d)
    except Exception:
        # Broadened from OSError: a lock/json/prune bug must never break a write.
        pass


def get_active(session_id: str | None = None) -> str | None:
    """Return the active board dir for `session_id`, or None if unset/gone.

    Tiered self-heal (#611): (1) if `session_id` has its own entry and that board
    still exists on disk → return it; else fall through. (2) `global.board` if it
    exists → return it (the previous single-pointer behaviour, used by a fresh /
    unseen session). (3) None, so the caller falls back (e.g. to mtime). A
    deleted board at one tier never blanks the resolution if a lower tier is
    live."""
    try:
        d = _read_active()
        if session_id:
            sb = (d["sessions"].get(session_id) or {}).get("board")
            if sb and Path(sb).exists():
                return sb
        gb = (d["global"] or {}).get("board")
        if gb and Path(gb).exists():
            return gb
    except Exception:
        return None
    return None


def _now_iso() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
