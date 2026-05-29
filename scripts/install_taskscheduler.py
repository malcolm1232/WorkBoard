#!/usr/bin/env python3
"""Install board-steward as a Windows Task Scheduler task so the server auto-starts on logon.

Windows counterpart to install_launchd.py / install_systemd.py. Same flags so the
install recipe stays platform-uniform: --project / --port / --status / --uninstall / --dry-run.

Uses schtasks.exe with an ONLOGON trigger and `pythonw.exe` (no console window).
The task runs in the current user's context — no admin elevation, matching the
VISION "no cloud, user owns the machine" principle.

Examples:
  install_taskscheduler.py --project C:\\Users\\me\\MyApp --port 7891
  install_taskscheduler.py --status
  install_taskscheduler.py --uninstall
  install_taskscheduler.py --dry-run        # print the schtasks command, run nothing (any OS)
"""
from __future__ import annotations
import argparse
import shutil
import subprocess
import sys
from pathlib import Path


TASK_FMT = "BoardSteward-{port}"


def find_pythonw() -> str:
    """Prefer pythonw.exe (no console window); fall back to python/python3."""
    for cand in ("pythonw.exe", "pythonw", "python.exe", "python", "python3"):
        p = shutil.which(cand)
        if p:
            return p
    # On non-Windows (dry-run preview) shutil.which may miss pythonw — return the name.
    return "pythonw.exe"


def build_command(serve_py: Path, project: Path, port: int) -> str:
    """The /TR run-string Task Scheduler executes on logon."""
    py = find_pythonw()
    return f'"{py}" "{serve_py}" --project "{project}" --port {port}'


def status(port: int) -> int:
    task = TASK_FMT.format(port=port)
    r = subprocess.run(["schtasks", "/Query", "/TN", task],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print(f"INSTALLED: {task}")
        print(r.stdout.strip())
        return 0
    print(f"NOT INSTALLED: {task}")
    return 1


def install(project: Path, port: int, dry_run: bool) -> None:
    serve_py = Path(__file__).resolve().parent / "serve.py"
    if not serve_py.exists():
        sys.exit(f"ERR: serve.py not found at {serve_py}")
    project = project.resolve()
    board_json = project / "board" / "board.json"
    if not board_json.exists() and not dry_run:
        print(f"WARN: {board_json} doesn't exist — server will bootstrap empty on first run")

    task = TASK_FMT.format(port=port)
    run = build_command(serve_py, project, port)
    # /SC ONLOGON = start at user logon; /RL LIMITED = no elevation; /F = overwrite.
    cmd = ["schtasks", "/Create", "/TN", task, "/TR", run,
           "/SC", "ONLOGON", "/RL", "LIMITED", "/F"]

    if dry_run:
        print(f"DRY-RUN: would register task {task}")
        print("  " + subprocess.list2cmdline(cmd))
        return

    if not sys.platform.startswith("win"):
        sys.exit("ERR: Task Scheduler is Windows-only. On macOS use install_launchd.py; "
                 "on Linux use install_systemd.py.")

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"registered: {task}")
        print(f"  → http://127.0.0.1:{port}/ will be live on every logon")
        print(f"  → start now without re-logon:  schtasks /Run /TN {task}")
        print(f"  → the task runs pythonw.exe (no console window)")
    else:
        print(f"WARN: schtasks /Create rc={r.returncode}")
        if r.stderr:
            print(r.stderr.rstrip())


def uninstall(port: int, dry_run: bool) -> None:
    task = TASK_FMT.format(port=port)
    if dry_run:
        print(f"DRY-RUN: would delete task {task}")
        return
    if not sys.platform.startswith("win"):
        sys.exit("ERR: Task Scheduler is Windows-only.")
    r = subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print(f"removed: {task}")
    else:
        print(f"NOT INSTALLED (or delete failed): {task}")
        if r.stderr:
            print(r.stderr.rstrip())


def main() -> None:
    ap = argparse.ArgumentParser(description="Install board-steward as a Task Scheduler task (Windows).")
    ap.add_argument("--project", type=Path, default=Path.cwd(),
                    help="Project root containing board/ (default: cwd)")
    ap.add_argument("--port", type=int, default=7891)
    ap.add_argument("--status", action="store_true", help="Show install state")
    ap.add_argument("--uninstall", action="store_true", help="Delete the task")
    ap.add_argument("--dry-run", action="store_true", help="Print the schtasks command; run nothing (any OS)")
    a = ap.parse_args()

    # --dry-run is allowed on any OS so the recipe can be previewed from a Mac/Linux box.
    if not a.dry_run and not sys.platform.startswith("win"):
        sys.exit("ERR: Task Scheduler is Windows-only. On macOS use install_launchd.py; "
                 "on Linux use install_systemd.py. (Use --dry-run to preview here.)")

    if a.status:
        sys.exit(status(a.port))
    if a.uninstall:
        uninstall(a.port, a.dry_run)
        return
    install(a.project, a.port, a.dry_run)


if __name__ == "__main__":
    main()
