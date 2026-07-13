"""Telegram capture config + per-user alias resolution (#856).

Aliases are NEVER hardcoded. They are derived from the user's own board
registry (the project folder name of each board they run), plus any custom
short aliases they set. An alias that is unknown or ambiguous resolves to
None: we do not guess which board an idea belongs to.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path

CONFIG_ENV = "BOARD_TELEGRAM_CONFIG"
DEFAULT_PATH = Path.home() / ".board-steward" / "telegram.json"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    return Path(env) if env else DEFAULT_PATH


def _read_raw() -> dict:
    """Whatever is on disk, without the usable-config gate. {} if missing/corrupt.

    Callers that only need to PRESERVE existing fields (patching the offset,
    reading custom aliases) must use this instead of load(): load() returns
    None for a corrupt or partial config, and treating that None as "empty"
    would silently discard the real token/chat_id/aliases already on disk.
    """
    p = config_path()
    if not p.exists():
        return {}
    try:
        conf = json.loads(p.read_text())
    except Exception:
        return {}
    return conf if isinstance(conf, dict) else {}


def load() -> dict | None:
    """The usable config, or None if not configured / corrupt / incomplete."""
    conf = _read_raw()
    if not conf or not conf.get("token") or not conf.get("chat_id"):
        return None
    return conf


def save(conf: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    # Open with mode 0600 from the very first byte on disk: the token is a
    # credential and must never be briefly world/group readable via the
    # process umask (os.chmod after write_text is too late).
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(conf, indent=2, sort_keys=True))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.replace(tmp, p)  # mode travels with the rename; no second chmod needed


def _patch(**fields) -> None:
    conf = _read_raw()
    conf.update(fields)
    save(conf)


def set_offset(offset: int) -> None:
    _patch(offset=int(offset))


def set_status(status: str | None) -> None:
    _patch(status=status)


def _slug(s: str) -> str:
    return _SLUG_RE.sub("", s.lower())


def _board_dirs() -> list[str]:
    try:
        import port_registry

        return list(port_registry.assignments().keys())
    except Exception:
        return []


def aliases() -> dict[str, str]:
    """alias -> board dir. Derived from the user's boards; custom aliases win.

    A folder name shared by two boards is dropped: it is ambiguous, so it must
    not resolve at all.
    """
    derived: dict[str, list[str]] = {}
    for d in _board_dirs():
        name = Path(d).parent.name  # ".../QuantifyMe/HFTAgents/board" -> "HFTAgents"
        derived.setdefault(_slug(name), []).append(d)
    out = {a: paths[0] for a, paths in derived.items() if len(paths) == 1}
    conf = _read_raw()
    for alias, d in (conf.get("aliases") or {}).items():
        out[_slug(alias)] = d
    return out


def resolve_board(alias: str | None) -> Path | None:
    """Exact match, then unique prefix. Ambiguous, blank or unknown returns None."""
    if not alias:
        return None
    a = _slug(alias)
    if not a:
        # A whitespace-only (or otherwise all-punctuation) alias slugs down to
        # "", and "".startswith("") is True for every alias in Python - that
        # would make a blank token match a single-board user's only alias.
        # Never guess: reject it here, after slugifying, not before.
        return None
    al = aliases()
    if a in al:
        return Path(al[a])
    hits = [d for name, d in al.items() if name.startswith(a)]
    if len(hits) == 1:
        return Path(hits[0])
    return None  # zero hits or ambiguous: never guess


def task_column(d: dict) -> str:
    """Where a routed capture lands: the task column, else task-like, else first."""
    cols = d.get("columns") or []
    for c in cols:
        if c.get("id") == "task":
            return "task"
    for c in cols:
        blob = f"{c.get('id', '')} {c.get('name', '')}".lower()
        if "task" in blob or "inbox" in blob or "todo" in blob:
            return c["id"]
    return cols[0]["id"] if cols else "task"
