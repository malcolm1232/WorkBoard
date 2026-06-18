#!/usr/bin/env python3
"""Hook fallback helper. Argv[1] = PWD. Stdout = board.json path, or empty.

When the SessionStart hook's CWD-walk fails, this helper searches
~/.claude/projects/*/*.jsonl for recent sessions whose recorded
cwd is DESCENDED FROM the current PWD (so a parent shell with a board
project under it gets picked up — but unrelated projects don't leak).
"""
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    try:
        pwd = Path(sys.argv[1]).resolve()
    except Exception:
        return 0

    sessions_root = Path.home() / ".claude" / "projects"
    if not sessions_root.is_dir():
        return 0

    files = sorted(
        sessions_root.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for jf in files[:30]:
        try:
            with jf.open(errors="replace") as f:
                for line in f:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    cwd = o.get("cwd")
                    if not cwd:
                        continue
                    try:
                        cwd_p = Path(cwd).resolve()
                    except Exception:
                        break
                    # Only accept cwds at PWD or descended from PWD.
                    if cwd_p == pwd or pwd in cwd_p.parents:
                        d = cwd_p
                        while True:
                            bp = d / "board" / "board.json"
                            if bp.is_file():
                                print(bp)
                                return 0
                            if d == pwd or d.parent == d:
                                break
                            d = d.parent
                    break
        except OSError:
            continue

    return 0


if __name__ == "__main__":
    sys.exit(main())
