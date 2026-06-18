#!/usr/bin/env python3
"""Cross-platform autostart dispatcher for board-steward.

Detects the OS via sys.platform and delegates to the right installer:
  - darwin  → install_launchd.py        (macOS launchd agent)
  - linux   → install_systemd.py        (systemd --user service)
  - win32   → install_taskscheduler.py  (Task Scheduler ONLOGON task)

This is the ONE command the install recipe points at, so the recipe stays
identical on every platform (VISION §3 "startup is instant and invisible"):

  python3 install_autostart.py --project <dir> --port 7891
  python3 install_autostart.py --status
  python3 install_autostart.py --uninstall

All flags (--project / --port / --status / --uninstall / --dry-run) pass straight
through to the platform installer, which owns validation and defaults.
"""
from __future__ import annotations
import sys
from pathlib import Path

DISPATCH = {
    "darwin": "install_launchd.py",
    "linux": "install_systemd.py",
    "win32": "install_taskscheduler.py",
}


def pick_installer() -> str:
    for prefix, script in DISPATCH.items():
        if sys.platform.startswith(prefix):
            return script
    sys.exit(f"ERR: unsupported platform {sys.platform!r}. "
             f"Supported: macOS (darwin), Linux, Windows (win32).")


def main() -> None:
    script = pick_installer()
    target = Path(__file__).resolve().parent / script
    if not target.exists():
        sys.exit(f"ERR: platform installer not found: {target}")

    # Hand off to the platform installer in-process so argparse/exit codes are
    # owned by exactly one module. runpy preserves __name__=='__main__' semantics
    # while keeping our (already-parsed-by-it) sys.argv intact.
    import runpy
    sys.argv[0] = str(target)
    print(f"[autostart] platform={sys.platform} → {script}", file=sys.stderr)
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
