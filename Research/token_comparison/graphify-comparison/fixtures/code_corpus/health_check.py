#!/usr/bin/env python3
"""board-steward health check — green/red dashboard for the autostart stack.

Verifies four layers in order:
  1. launchd (or other autostart) has a live PID per registered port
  2. /health responds on each port with a sane rev + card count
  3. SessionStart hook is installed in ~/.claude/settings.json
  4. SessionStart hook actually fired in the most recent Claude session
     (greps ~/.claude/projects/*/*.jsonl for the marker)

Exit code is 0 only when all four pass. Designed to run in <1s.

Usage:
    python health_check.py                  # check all registered ports
    python health_check.py --port 7891      # check just one
    python health_check.py --json           # machine-readable
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

LAUNCHAGENTS = Path.home() / "Library/LaunchAgents"
SETTINGS = Path.home() / ".claude/settings.json"
PROJECTS = Path.home() / ".claude/projects"
HOOK_MARKER = "board-steward-session-start"
PLIST_RX = re.compile(r"com\.boardsteward\.(\d+)\.plist$")


def discover_ports() -> list[int]:
    if not LAUNCHAGENTS.is_dir():
        return []
    ports = []
    for p in LAUNCHAGENTS.iterdir():
        m = PLIST_RX.search(p.name)
        if m:
            ports.append(int(m.group(1)))
    return sorted(ports)


def launchctl_pid(port: int) -> tuple[bool, int | None, int | None]:
    """Return (loaded, pid_or_None, last_exit_or_None) for the plist."""
    label = f"com.boardsteward.{port}"
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=3
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return (False, None, None)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] == label:
            pid = None if parts[0] == "-" else int(parts[0])
            try:
                last = int(parts[1])
            except ValueError:
                last = None
            return (True, pid, last)
    return (False, None, None)


def fetch_health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=2
        ) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError, ValueError):
        return None


def hook_installed() -> tuple[bool, str | None]:
    if not SETTINGS.is_file():
        return (False, None)
    try:
        cfg = json.loads(SETTINGS.read_text())
    except json.JSONDecodeError:
        return (False, None)
    sessions = (cfg.get("hooks") or {}).get("SessionStart") or []
    for entry in sessions:
        for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
            cmd = hook.get("command") or ""
            if "board-steward" in cmd or "hook_session_start" in cmd:
                return (True, cmd)
    return (False, None)


def latest_session_log() -> Path | None:
    if not PROJECTS.is_dir():
        return None
    candidates = []
    for proj in PROJECTS.iterdir():
        sess_dir = proj
        for f in sess_dir.glob("*.jsonl"):
            candidates.append(f)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def hook_fired_recently() -> tuple[bool, float | None, Path | None]:
    """Find marker in newest session jsonl. Returns (fired, age_seconds, path)."""
    log = latest_session_log()
    if not log:
        return (False, None, None)
    age = datetime.datetime.now().timestamp() - log.stat().st_mtime
    # quick byte-level grep — these files can be MB
    try:
        with open(log, "rb") as f:
            data = f.read()
    except OSError:
        return (False, None, log)
    fired = HOOK_MARKER.encode() in data
    return (fired, age, log)


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{int(seconds / 86400)}d ago"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, action="append",
                    help="Check this port (repeatable). Default: discover all boardsteward plists.")
    ap.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = ap.parse_args()

    ports = args.port or discover_ports()
    result = {"ports": [], "hookInstalled": False, "hookFired": False, "ok": True}

    if not ports:
        if not args.json:
            print("✗ no boardsteward launchd plists found at ~/Library/LaunchAgents/")
            print("  run install_launchd.py --project <dir> --port <port>")
        result["ok"] = False

    for port in ports:
        loaded, pid, last_rc = launchctl_pid(port)
        health = fetch_health(port)
        entry = {
            "port": port,
            "launchdLoaded": loaded,
            "pid": pid,
            "lastExit": last_rc,
            "serverUp": health is not None,
            "rev": (health or {}).get("rev"),
            "cards": (health or {}).get("cards"),
            "sseClients": (health or {}).get("sseClients"),
            "project": (health or {}).get("project"),
        }
        if not (loaded and pid and health):
            result["ok"] = False
        result["ports"].append(entry)

    installed, cmd = hook_installed()
    result["hookInstalled"] = installed
    result["hookCommand"] = cmd
    if not installed:
        result["ok"] = False

    fired, age, log = hook_fired_recently()
    result["hookFired"] = fired
    result["hookLastSeenSecondsAgo"] = age
    result["hookLogPath"] = str(log) if log else None
    if not fired:
        result["ok"] = False

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    # human dashboard
    def mark(ok: bool) -> str:
        return "✅" if ok else "✗"

    for e in result["ports"]:
        line = (
            f"{mark(e['launchdLoaded'] and e['pid'])} launchd:{e['port']}  "
            f"pid={e['pid'] or '-':<6} "
            f"{mark(e['serverUp'])} /health rev={e['rev'] or '?'} cards={e['cards'] or '?'} "
            f"sse={e['sseClients'] or 0}"
        )
        if e["project"]:
            line += f"  {Path(e['project']).name}"
        print(line)

    print(f"{mark(installed)} SessionStart hook installed"
          + (f"  → {Path(cmd.split()[-1]).name}" if cmd else ""))
    print(f"{mark(fired)} hook fired in last session ({fmt_age(age)})"
          + (f"  log={log.name}" if log else ""))

    print()
    print("✅ ALL GREEN — ready to ship" if result["ok"]
          else "✗ ONE OR MORE CHECKS FAILED — see lines marked ✗ above")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
