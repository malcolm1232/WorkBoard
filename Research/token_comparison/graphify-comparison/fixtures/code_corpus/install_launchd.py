#!/usr/bin/env python3
"""Install board-steward as a macOS launchd agent so the server auto-starts on login.

Idempotent. Backs up existing plist before overwriting. Honors --dry-run.

Examples:
  install_launchd.py --project ~/Desktop/MyProject
  install_launchd.py --status
  install_launchd.py --uninstall
"""
from __future__ import annotations
import argparse
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path


LABEL_FMT = "com.boardsteward.{port}"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "boardsteward"


def find_python() -> str:
    for cand in ("python3", "/usr/bin/python3"):
        if cand.startswith("/"):
            if Path(cand).exists():
                return cand
        else:
            p = shutil.which(cand)
            if p:
                return p
    sys.exit("ERR: python3 not found on PATH")


def plist_path(port: int) -> Path:
    return PLIST_DIR / f"{LABEL_FMT.format(port=port)}.plist"


def build_plist(serve_py: Path, project: Path, port: int) -> dict:
    return {
        "Label": LABEL_FMT.format(port=port),
        "ProgramArguments": [
            find_python(),
            str(serve_py),
            "--project", str(project),
            "--port", str(port),
        ],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_DIR / f"boardsteward-{port}.out.log"),
        "StandardErrorPath": str(LOG_DIR / f"boardsteward-{port}.err.log"),
    }


def status(port: int) -> int:
    p = plist_path(port)
    if not p.exists():
        print(f"NOT INSTALLED ({p} missing)")
        return 1
    label = LABEL_FMT.format(port=port)
    r = subprocess.run(["launchctl", "list", label], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"INSTALLED + LOADED: {label}")
        print(r.stdout.strip())
        return 0
    print(f"INSTALLED but NOT LOADED: {label}")
    return 2


def install(project: Path, port: int, dry_run: bool) -> None:
    serve_py = Path(__file__).resolve().parent / "serve.py"
    if not serve_py.exists():
        sys.exit(f"ERR: serve.py not found at {serve_py}")
    project = project.resolve()
    board_json = project / "board" / "board.json"
    if not board_json.exists() and not dry_run:
        print(f"WARN: {board_json} doesn't exist — server will bootstrap empty on first run")

    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    data = build_plist(serve_py, project, port)
    target = plist_path(port)

    if dry_run:
        print(f"DRY-RUN: would write {target}")
        print(plistlib.dumps(data).decode())
        return

    if target.exists():
        bak = target.with_suffix(f".plist.bak-{int(time.time())}")
        shutil.copy2(target, bak)
        print(f"backed up: {bak}")
        subprocess.run(["launchctl", "unload", str(target)], capture_output=True)

    with open(target, "wb") as f:
        plistlib.dump(data, f)
    print(f"wrote: {target}")

    r = subprocess.run(["launchctl", "load", str(target)], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"loaded: {LABEL_FMT.format(port=port)}")
        print(f"  → http://127.0.0.1:{port}/ will be live now and on every login")
        print(f"  → logs: {LOG_DIR}/boardsteward-{port}.{{out,err}}.log")
    else:
        print(f"WARN: launchctl load rc={r.returncode}")
        if r.stderr:
            print(r.stderr.rstrip())


def uninstall(port: int, dry_run: bool) -> None:
    target = plist_path(port)
    if not target.exists():
        print(f"NOT INSTALLED: {target}")
        return
    if dry_run:
        print(f"DRY-RUN: would unload + remove {target}")
        return
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True)
    target.unlink()
    print(f"removed: {target}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Install board-steward as a launchd agent (macOS).")
    ap.add_argument("--project", type=Path, default=Path.cwd(),
                    help="Project root containing board/ (default: cwd)")
    ap.add_argument("--port", type=int, default=7891)
    ap.add_argument("--status", action="store_true", help="Show install + load state")
    ap.add_argument("--uninstall", action="store_true", help="Unload + delete plist")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if sys.platform != "darwin":
        sys.exit("ERR: launchd is macOS-only. On Linux use a systemd --user unit.")

    if a.status:
        sys.exit(status(a.port))
    if a.uninstall:
        uninstall(a.port, a.dry_run)
        return
    install(a.project, a.port, a.dry_run)


if __name__ == "__main__":
    main()
