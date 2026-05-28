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

import json
import os
from pathlib import Path


REGISTRY_ENV = "BOARD_REGISTRY"

# Public for tests / inspectors. Lives directly in $HOME (not ~/.config,
# which on macOS is often root-owned and unwritable for normal users).
DEFAULT_PATH = Path.home() / ".board-steward" / "port-registry.json"


def registry_path() -> Path:
    env = os.environ.get(REGISTRY_ENV)
    if env:
        return Path(env).expanduser()
    return DEFAULT_PATH


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


def write(board_dir: str | os.PathLike, port: int, pid: int) -> None:
    """Register THIS process as serving `board_dir` on `port`. Idempotent.

    Stomps any prior entry for the same board path (the prior server is
    presumed dead — only one launchd plist exists per port, so a new boot
    overrides any stale row)."""
    key = str(Path(board_dir).resolve())
    d = read()
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


def _now_iso() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
