#!/usr/bin/env python3
"""Install board-steward as a systemd --user service so the server auto-starts on login.

Linux counterpart to install_launchd.py. Same flags so the install recipe stays
platform-uniform: --project / --port / --status / --uninstall / --dry-run.

A --user unit (not system) means no sudo and no root daemon — it follows the
VISION "no cloud, the user owns their machine" principle. `loginctl enable-linger`
is suggested (not forced) so the board survives logout without a login session.

Examples:
  install_systemd.py --project ~/projects/MyApp --port 7891
  install_systemd.py --status
  install_systemd.py --uninstall
  install_systemd.py --dry-run            # print the unit, write nothing (works on any OS)
"""
from __future__ import annotations
import argparse
import shutil
import subprocess
import sys
from pathlib import Path


UNIT_FMT = "boardsteward-{port}.service"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


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


def unit_path(port: int) -> Path:
    return UNIT_DIR / UNIT_FMT.format(port=port)


def build_unit(serve_py: Path, project: Path, port: int) -> str:
    py = find_python()
    return (
        "[Unit]\n"
        "Description=board-steward kanban server (port {port})\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart={py} {serve} --project {project} --port {port}\n"
        "WorkingDirectory={project}\n"
        "Restart=always\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).format(py=py, serve=serve_py, project=project, port=port)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def status(port: int) -> int:
    p = unit_path(port)
    if not p.exists():
        print(f"NOT INSTALLED ({p} missing)")
        return 1
    unit = UNIT_FMT.format(port=port)
    active = _systemctl("is-active", unit).stdout.strip()
    enabled = _systemctl("is-enabled", unit).stdout.strip()
    if active == "active":
        print(f"INSTALLED + RUNNING: {unit} (enabled={enabled})")
        return 0
    print(f"INSTALLED but {active or 'not-running'}: {unit} (enabled={enabled})")
    return 2


def install(project: Path, port: int, dry_run: bool) -> None:
    serve_py = Path(__file__).resolve().parent / "serve.py"
    if not serve_py.exists():
        sys.exit(f"ERR: serve.py not found at {serve_py}")
    project = project.resolve()
    board_json = project / "board" / "board.json"
    if not board_json.exists() and not dry_run:
        print(f"WARN: {board_json} doesn't exist — server will bootstrap empty on first run")

    unit_text = build_unit(serve_py, project, port)
    target = unit_path(port)

    if dry_run:
        print(f"DRY-RUN: would write {target}")
        print(unit_text)
        return

    if sys.platform.startswith("win"):
        sys.exit("ERR: systemd is Linux-only. On Windows use install_taskscheduler.py.")

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    if target.exists():
        bak = target.with_name(target.name + ".bak")
        shutil.copy2(target, bak)
        print(f"backed up: {bak}")

    target.write_text(unit_text)
    print(f"wrote: {target}")

    unit = UNIT_FMT.format(port=port)
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", unit)
    if r.returncode == 0:
        print(f"enabled + started: {unit}")
        print(f"  → http://127.0.0.1:{port}/ live now and on every login")
        print(f"  → logs: journalctl --user -u {unit} -f")
        print(f"  → survive logout (optional): loginctl enable-linger {Path.home().name}")
    else:
        print(f"WARN: systemctl enable --now rc={r.returncode}")
        if r.stderr:
            print(r.stderr.rstrip())
        print("  (no systemd --user session? try: install on the desktop login, or use linger)")


def uninstall(port: int, dry_run: bool) -> None:
    target = unit_path(port)
    if not target.exists():
        print(f"NOT INSTALLED: {target}")
        return
    if dry_run:
        print(f"DRY-RUN: would stop+disable+remove {target}")
        return
    unit = UNIT_FMT.format(port=port)
    _systemctl("disable", "--now", unit)
    target.unlink()
    _systemctl("daemon-reload")
    print(f"removed: {target}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Install board-steward as a systemd --user service (Linux).")
    ap.add_argument("--project", type=Path, default=Path.cwd(),
                    help="Project root containing board/ (default: cwd)")
    ap.add_argument("--port", type=int, default=7891)
    ap.add_argument("--status", action="store_true", help="Show install + run state")
    ap.add_argument("--uninstall", action="store_true", help="Stop, disable + delete unit")
    ap.add_argument("--dry-run", action="store_true", help="Print the unit; write nothing (any OS)")
    a = ap.parse_args()

    # --dry-run is allowed on any OS so the recipe can be previewed from a Mac.
    if not a.dry_run and not sys.platform.startswith("linux"):
        sys.exit("ERR: systemd is Linux-only. On macOS use install_launchd.py; "
                 "on Windows use install_taskscheduler.py. (Use --dry-run to preview here.)")

    if a.status:
        sys.exit(status(a.port))
    if a.uninstall:
        uninstall(a.port, a.dry_run)
        return
    install(a.project, a.port, a.dry_run)


if __name__ == "__main__":
    main()
