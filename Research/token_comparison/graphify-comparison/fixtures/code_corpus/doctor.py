#!/usr/bin/env python3
"""board-steward install-doctor (#572) — diagnose why the plugin's hooks aren't
firing, and offer the one-line fix.

The #1 failure mode after a reinstall/churn: the plugin ends up installed but
NOT enabled — `enabledPlugins` in ~/.claude/settings.json loses its
`board-steward@workboard: true` entry, so 0 hooks load and the board never
opens. The plugin cannot self-enable (enablement is Claude Code's), so the cure
is a settings.json edit. This doctor finds that (and a few neighbours) and, with
--fix, repairs the enable entry in place.

Usage:
  python3 doctor.py          # diagnose, print PASS/WARN/FAIL + remediation
  python3 doctor.py --fix    # also re-add the enabledPlugins entry if missing
Exit code: 0 if no FAIL, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

PLUGIN_KEY = "board-steward@workboard"
SETTINGS = Path.home() / ".claude" / "settings.json"
DOT_BOARD = Path.home() / ".board-steward"
CACHE_GLOB = "plugins/cache/*/board-steward"
INSTALLED = Path.home() / ".claude" / "plugins" / "installed_plugins.json"

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def check_enabled(fix: bool) -> tuple[str, str, str]:
    """The big one: is board-steward in enabledPlugins=true? This is what
    decides whether the 7 hooks load."""
    data = _load_json(SETTINGS)
    if data is None:
        return ("FAIL", "settings.json unreadable/missing",
                f"create {SETTINGS} with an enabledPlugins block")
    enabled = data.get("enabledPlugins") or {}
    if enabled.get(PLUGIN_KEY) is True:
        return ("PASS", f"{PLUGIN_KEY} is enabled in settings.json", "")
    # missing or false → THE 0-hooks cause
    if fix:
        enabled[PLUGIN_KEY] = True
        data["enabledPlugins"] = enabled
        try:
            SETTINGS.write_text(json.dumps(data, indent=2) + "\n")
            return ("PASS", f"{PLUGIN_KEY} was disabled — FIXED (set true)",
                    "run /reload-plugins (or restart) → expect 7 hooks")
        except OSError as e:
            return ("FAIL", f"{PLUGIN_KEY} disabled; auto-fix failed: {e}",
                    f'add "{PLUGIN_KEY}": true to enabledPlugins in {SETTINGS}')
    return ("FAIL", f"{PLUGIN_KEY} NOT enabled → 0 hooks load (the disable bug)",
            f'rerun with --fix, or add "{PLUGIN_KEY}": true to '
            f"enabledPlugins in {SETTINGS}, then /reload-plugins")


def check_installed() -> tuple[str, str, str]:
    data = _load_json(INSTALLED)
    blob = json.dumps(data) if data is not None else ""
    if "board-steward" in blob:
        return ("PASS", "plugin present in installed_plugins.json", "")
    return ("FAIL", "plugin not in installed_plugins.json",
            "/plugin install board-steward@workboard")


def check_cache() -> tuple[str, str, str]:
    hits = list((Path.home() / ".claude").glob(CACHE_GLOB))
    if hits:
        ver = sorted(p.name for p in hits[0].glob("*") if p.is_dir())
        return ("PASS", f"plugin cache present ({hits[0].name}{'/' + ver[-1] if ver else ''})", "")
    return ("WARN", "no plugin cache dir",
            "reinstall to populate cache: /plugin install board-steward@workboard")


def check_runtime() -> tuple[str, str, str]:
    """Onboarded marker + a live board server (informational — absent is normal
    for a brand-new user before first-run)."""
    if not DOT_BOARD.exists():
        return ("WARN", "~/.board-steward absent (not onboarded yet)",
                "expected for a fresh user; the first-run picker will create it")
    # try the registered port(s) quickly
    for port in (7891, 7892, 7893):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=0.4) as r:
                d = json.loads(r.read().decode())
                return ("PASS", f"board server live on :{port} "
                        f"(rev {d.get('rev')}, {d.get('cards')} cards)", "")
        except Exception:
            continue
    return ("WARN", "onboarded but no live board server on 7891-7893",
            "start a session in a board project, or it auto-spawns on next prompt")


def main() -> int:
    fix = "--fix" in sys.argv
    print(f"{DIM}board-steward install-doctor{' (--fix)' if fix else ''}{RESET}\n")
    checks = [
        ("plugin enabled (hooks load)", check_enabled(fix)),
        ("plugin installed", check_installed()),
        ("plugin cache", check_cache()),
        ("runtime (onboarded + server)", check_runtime()),
    ]
    worst_fail = False
    for name, (status, msg, fixhint) in checks:
        color = {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED}[status]
        worst_fail = worst_fail or status == "FAIL"
        print(f"  {color}{status:4}{RESET}  {name}: {msg}")
        if fixhint:
            print(f"        {DIM}↳ {fixhint}{RESET}")
    print()
    if worst_fail:
        print(f"{RED}✗ install has a blocking problem{RESET} — "
              f"see the ↳ fix above"
              f"{' (rerun with --fix to auto-repair the enable entry)' if not fix else ''}")
        return 1
    print(f"{GREEN}✓ board-steward install looks healthy{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
