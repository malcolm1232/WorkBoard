"""Telegram capture config + per-user alias resolution (#856).

Aliases are NEVER hardcoded. They are derived from the user's own board
registry (the project folder name of each board they run), plus any custom
short aliases they set. An alias that is unknown or ambiguous resolves to
None: we do not guess which board an idea belongs to.
"""
from __future__ import annotations

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


def load() -> dict | None:
    p = config_path()
    if not p.exists():
        return None
    try:
        conf = json.loads(p.read_text())
    except Exception:
        return None
    if not conf.get("token") or not conf.get("chat_id"):
        return None
    return conf


def save(conf: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)  # the token is a credential: never group/world readable
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def _patch(**fields) -> None:
    conf = load() or {}
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
    conf = load() or {}
    for alias, d in (conf.get("aliases") or {}).items():
        out[_slug(alias)] = d
    return out


def resolve_board(alias: str | None) -> Path | None:
    """Exact match, then unique prefix. Ambiguous or unknown returns None."""
    if not alias:
        return None
    a = _slug(alias)
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
