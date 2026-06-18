#!/usr/bin/env python3
"""
_hook_flash_linked.py — PreToolUse helper for #102 BOARD-AUTO-LINK.

Reads a JSON event on stdin (Claude Code PreToolUse payload), looks at the
edited file path, walks up from that file (then PWD) for board/board.json, finds cards whose
linkedFiles include that path (or its basename), and pings /flash?card=N&file=…
on the board server. Fire-and-forget — any error stays silent so the hook
never delays Claude's Edit/Write.

Designed to complete in <80ms on a warm filesystem. Hard 0.3s curl timeout
per ping. Exits 0 always.

Usage (from hook_pre_tool_use.sh):
    python3 _hook_flash_linked.py "$PWD"
    (stdin is the PreToolUse JSON Claude Code emits)
"""
from __future__ import annotations
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Make the sibling port_registry importable whether this is run as a script
# (sys.path[0] = its dir) or invoked with an absolute path from the hook.
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)


# Fallback probe window when the registry has no live entry for this board.
# The registry (port_registry.lookup/assign) is the primary resolver — a board
# can sit anywhere in 7891-7999, so a fixed 5-port tuple would miss board #6+.
PROBE_LO, PROBE_HI = 7891, 7900
PING_TIMEOUT_S = 0.3
MAX_HOOK_BUDGET_S = 0.8  # total wall time we'll spend before bailing


def find_board(start: Path) -> Path | None:
    """Walk up from start (max 8 levels) looking for board/board.json."""
    p = start.resolve()
    for _ in range(8):
        cand = p / "board" / "board.json"
        if cand.is_file():
            return cand
        if p.parent == p:
            break
        p = p.parent
    return None


def normalise(file_path: str, project_root: Path) -> str:
    """Make `file_path` absolute, resolving relative paths against the
    project root (the directory containing board/)."""
    fp = Path(file_path)
    if not fp.is_absolute():
        fp = project_root / fp
    try:
        return str(fp.resolve())
    except Exception:
        return str(fp)


def matching_cards(board_path: Path, file_abs: str) -> list[dict]:
    """Return cards whose linkedFiles contains file_abs (or its basename
    in a linkedFile basename collision). Reads board.json directly; no HTTP."""
    try:
        d = json.loads(board_path.read_text())
    except Exception:
        return []
    base = Path(file_abs).name
    hits = []
    for c in d.get("cards", []):
        lf = c.get("linkedFiles") or []
        if not lf:
            continue
        if file_abs in lf:
            hits.append(c)
            continue
        if any(Path(x).name == base and Path(x).name for x in lf):
            # Basename match — looser, but catches "card linked the rel path,
            # Claude edited via abs path" and vice versa.
            hits.append(c)
    return hits


def _port_owns_board(port: int, target: str) -> bool:
    """True iff the server on `port` answers /health naming `target` board dir."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=PING_TIMEOUT_S,
        ) as resp:
            health = json.load(resp)
        return str(Path(health.get("board") or "").resolve()) == target
    except Exception:
        return False


def live_port_for(board_path: Path) -> int | None:
    """Resolve the live local port serving this board.

    Registry-first (handles any board in 7891-7999, not just the first five):
    look up the board's designated port and verify via /health. Only if that
    misses do we fall back to probing PROBE_LO..PROBE_HI — the safety net for an
    unregistered ad-hoc server. Mirrors card_state._resolve_server_url."""
    target = str(board_path.parent.resolve())
    try:
        import port_registry
        cached = port_registry.lookup(board_path.parent)
        if cached and _port_owns_board(cached, target):
            return cached
    except Exception:
        pass  # registry unavailable → probe fallback
    for port in range(PROBE_LO, PROBE_HI + 1):
        if _port_owns_board(port, target):
            return port
    return None


def ping_flash(port: int, card_num: int, file_abs: str) -> None:
    qs = urllib.parse.urlencode({"card": str(card_num), "file": file_abs})
    url = f"http://127.0.0.1:{port}/flash?{qs}"
    try:
        urllib.request.urlopen(url, timeout=PING_TIMEOUT_S).read()
    except Exception:
        pass


def main() -> int:
    pwd = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

    # Read PreToolUse JSON from stdin. Schemas vary across Claude Code
    # versions; we look for tool_input.file_path / file / path liberally.
    try:
        raw = sys.stdin.read(20000)  # cap stdin so we don't hang on weirdness
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    tool = (event.get("tool_name") or event.get("tool") or "").lower()
    if tool not in ("edit", "write", "multiedit", "notebookedit"):
        return 0

    ti = event.get("tool_input") or event.get("input") or {}
    fp = (
        ti.get("file_path")
        or ti.get("filePath")
        or ti.get("path")
        or ti.get("notebook_path")
    )
    if not fp:
        return 0

    # Locate the board by walking UP from the edited file first — the file is
    # inside the project, whereas PWD may be a *parent* of the project (e.g.
    # Claude Code launched from $HOME while the board lives in a subdir). Fall
    # back to PWD only if the file-anchored search comes up empty.
    fp_anchor = Path(fp)
    if not fp_anchor.is_absolute():
        fp_anchor = pwd / fp_anchor
    board_path = find_board(fp_anchor.parent) or find_board(pwd)
    if board_path is None:
        return 0

    project_root = board_path.parent.parent
    file_abs = normalise(fp, project_root)
    hits = matching_cards(board_path, file_abs)
    if not hits:
        return 0

    port = live_port_for(board_path)
    if port is None:
        return 0

    # Cap fan-out: flash up to 4 cards so a wildly-overlinked file doesn't
    # explode the toast stack.
    for c in hits[:4]:
        n = c.get("num")
        if isinstance(n, int):
            ping_flash(port, n, file_abs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
