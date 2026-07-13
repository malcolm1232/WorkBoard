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
import tempfile
from pathlib import Path

CONFIG_ENV = "BOARD_TELEGRAM_CONFIG"
DEFAULT_PATH = Path.home() / ".board-steward" / "telegram.json"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class _ReadFailed(Exception):
    """Private sentinel: the config file exists but could not be read (a
    chmod 000 file, a cloud-sync lock, an unsearchable parent dir - any
    OSError). The bytes on disk may be a perfectly good config; callers must
    not treat this the same as "missing or corrupt" and must never overwrite
    the file while it is in this state.
    """


def config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    return Path(env) if env else DEFAULT_PATH


def _read_raw() -> dict:
    """Whatever is on disk, without the usable-config gate.

    Returns {} when the file is missing, or present but corrupt/unparseable/
    not a JSON object - in those cases there is nothing recoverable, so
    treating it as empty (and letting a later save() overwrite it) is safe.

    Raises _ReadFailed when the file exists but could not be read at all
    (any OSError, including an unsearchable parent directory surfacing via
    Path.exists()). The contents are unknown but may be intact, so the
    caller must not overwrite them.

    Callers that only need to PRESERVE existing fields (patching the offset,
    reading custom aliases) must use this instead of load(): load() returns
    None for a corrupt or partial config, and treating that None as "empty"
    would silently discard the real token/chat_id/aliases already on disk.
    """
    p = config_path()
    try:
        if not p.exists():
            return {}
        text = p.read_text()
    except OSError as e:
        raise _ReadFailed(str(e)) from e
    try:
        conf = json.loads(text)
    except Exception:
        return {}
    return conf if isinstance(conf, dict) else {}


def load() -> dict | None:
    """The usable config, or None if not configured / corrupt / incomplete /
    unreadable."""
    try:
        conf = _read_raw()
    except _ReadFailed:
        return None
    if not conf or not conf.get("token") or not conf.get("chat_id"):
        return None
    return conf


def save(conf: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp always CREATES a fresh file at mode 0600 (no reuse of a stale
    # leftover at a fixed path, which could still be sitting at a lax mode
    # from a crashed prior write and would carry that mode onto the real
    # config via os.replace).
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(conf, indent=2, sort_keys=True))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.replace(tmp, p)  # mode travels with the rename; no second chmod needed


def _patch(**fields) -> None:
    """Merge fields into the on-disk config and save it - unless the
    existing file could not be read, in which case do nothing.

    This is what a scheduled, unattended caller (set_offset, set_status)
    relies on: "I couldn't read this" must never become "so I'll overwrite
    it", or a perfectly good token sitting behind a permission error would
    be destroyed.
    """
    try:
        conf = _read_raw()
    except _ReadFailed:
        return
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
    not resolve at all. The same rule applies to custom aliases: if two
    different custom alias strings slugify to the same thing but point at
    different boards, that slug is ambiguous and dropped too (never guess
    which board it meant). A custom alias overriding a derived one is still a
    deliberate override, not an ambiguity - and two custom aliases that share
    a slug but agree on the target board are redundant, not ambiguous.
    """
    derived: dict[str, list[str]] = {}
    for d in _board_dirs():
        name = Path(d).parent.name  # ".../QuantifyMe/HFTAgents/board" -> "HFTAgents"
        derived.setdefault(_slug(name), []).append(d)
    out = {a: paths[0] for a, paths in derived.items() if len(paths) == 1}

    try:
        conf = _read_raw()
    except _ReadFailed:
        # Custom aliases are unavailable while the file is unreadable; the
        # derived aliases are still safe to serve. Degrade, don't crash.
        conf = {}
    custom: dict[str, set[str]] = {}
    for alias, d in (conf.get("aliases") or {}).items():
        custom.setdefault(_slug(alias), set()).add(d)
    for slug, targets in custom.items():
        if len(targets) == 1:
            out[slug] = next(iter(targets))
        else:
            out.pop(slug, None)  # two custom aliases disagree: never guess
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
