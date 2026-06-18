#!/usr/bin/env python3
"""
install_hooks.py — idempotently wire board-steward hooks into Claude Code settings.

By default, installs a SessionStart hook (fires once per Claude session, injects
a tight digest into context). This is the recommended primitive — fires reliably
regardless of CWD/first-prompt timing, and doesn't bleed tokens on every prompt.

The older UserPromptSubmit hook is still available (--hook user-prompt-submit)
for users who want per-prompt protocol nudges. `--hook both` installs both.

Usage:
    install_hooks.py --hook live                  # RECOMMENDED: SessionStart + UserPromptSubmit + Stop (always-on LIVE enforcement, #359/#360)
    install_hooks.py                              # install SessionStart only
    install_hooks.py --hook user-prompt-submit    # per-prompt hook alone
    install_hooks.py --hook both                  # session-start + user-prompt-submit (legacy)
    install_hooks.py --dry-run
    install_hooks.py --uninstall                  # remove ALL board-steward hooks
    install_hooks.py --status                     # report current state

Resolves its own location via __file__, so the hook command path in
settings.json points to wherever the skill happens to live on this machine.

Safe to run repeatedly — flips the configured set to match --hook by
adding selected variants and removing unselected ones.
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


HOOK_VARIANTS = {
    "session-start":      ("SessionStart",      "hook_session_start.sh"),
    "user-prompt-submit": ("UserPromptSubmit",  "hook_user_prompt.sh"),
    "pre-tool-use":       ("PreToolUse",        "hook_pre_tool_use.sh"),
    "card-before-edit":   ("PreToolUse",        "hook_card_before_edit.sh"),
    "stop":               ("Stop",              "hook_stop.sh"),
}
ALL_VARIANTS = tuple(HOOK_VARIANTS.keys())
# PreToolUse needs a matcher so we only fire on file-mutating tools.
HOOK_MATCHERS = {
    "PreToolUse": "Edit|Write|MultiEdit|NotebookEdit",
}


def claude_settings_path() -> Path:
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(env_dir) if env_dir else Path.home() / ".claude"
    return base / "settings.json"


def hook_command(script_name: str) -> str:
    return str((Path(__file__).resolve().parent / script_name).resolve())


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"refusing to touch malformed settings.json ({path}): {e}")


def find_existing(settings: dict, event: str, cmd: str) -> tuple[int, int] | None:
    hooks = settings.get("hooks", {}).get(event, [])
    for gi, group in enumerate(hooks):
        for hi, hook in enumerate(group.get("hooks", [])):
            if hook.get("type") == "command" and hook.get("command") == cmd:
                return gi, hi
    return None


def _same_script(installed_cmd: str, target_cmd: str) -> bool:
    """True if an installed hook command refers to the SAME board-steward hook
    script as target_cmd, regardless of install path — matched by basename
    (e.g. 'hook_session_start.sh'). Matching on the full recomputed absolute
    path alone missed hooks wired under a previous/relocated skill dir (#371)."""
    return os.path.basename(installed_cmd or "") == os.path.basename(target_cmd)


def add_hook(settings: dict, event: str, cmd: str) -> str:
    if find_existing(settings, event, cmd) is not None:
        return "already-installed"
    # If the same hook script is wired at a DIFFERENT path (relocated / old
    # install dir), replace it instead of appending a duplicate (#371).
    relinked = any(
        h.get("type") == "command" and _same_script(h.get("command"), cmd)
        and h.get("command") != cmd
        for group in settings.get("hooks", {}).get(event, [])
        for h in group.get("hooks", [])
    )
    if relinked:
        remove_hook(settings, event, cmd)
    settings.setdefault("hooks", {}).setdefault(event, []).append({
        "matcher": HOOK_MATCHERS.get(event, ""),
        "hooks": [{"type": "command", "command": cmd}],
    })
    return "relinked" if relinked else "installed"


def remove_hook(settings: dict, event: str, cmd: str) -> str:
    hooks = settings.get("hooks", {}).get(event, [])
    removed = False
    new_groups = []
    for group in hooks:
        new_hooks = [h for h in group.get("hooks", [])
                     if not (h.get("type") == "command" and _same_script(h.get("command"), cmd))]
        if len(new_hooks) != len(group.get("hooks", [])):
            removed = True
        if new_hooks:
            new_groups.append({**group, "hooks": new_hooks})
    if not removed:
        return "not-installed"
    if new_groups:
        settings["hooks"][event] = new_groups
    else:
        settings["hooks"].pop(event, None)
        if not settings["hooks"]:
            settings.pop("hooks", None)
    return "uninstalled"


def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bk = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, bk)
    return bk


def cmd_status() -> int:
    path = claude_settings_path()
    settings = load_settings(path)
    print(f"settings.json: {path} {'(exists)' if path.exists() else '(missing)'}")
    any_installed = False
    for name, (event, script) in HOOK_VARIANTS.items():
        cmd = hook_command(script)
        exists = Path(cmd).exists() and os.access(cmd, os.X_OK)
        installed = find_existing(settings, event, cmd) is not None
        marker = "✓" if installed else "✗"
        print(f"  {marker} {name:22s}  event={event:18s}  script_ok={exists}  installed={installed}")
        if installed:
            any_installed = True
    return 0 if any_installed else 1


def apply_selection(settings: dict, selected: set[str],
                    dry_run: bool = False) -> tuple[dict, list[str]]:
    log = []
    for name, (event, script) in HOOK_VARIANTS.items():
        cmd = hook_command(script)
        if name in selected:
            if not Path(cmd).exists():
                sys.exit(f"hook script missing at {cmd} — re-install the skill, then retry")
            # Claude Code runs the hook by bare path, so it MUST be executable.
            # git/rsync/zip can strip the +x bit — enforce it here so a fresh
            # install never wires a hook that silently fails to fire.
            if not os.access(cmd, os.X_OK):
                if dry_run:
                    log.append(f"  {name:22s} → would chmod +x (not executable)")
                else:
                    try:
                        mode = os.stat(cmd).st_mode
                        os.chmod(cmd, mode | 0o111)  # +x for u,g,o
                        log.append(f"  {name:22s} → chmod +x (was not executable)")
                    except OSError as e:
                        sys.exit(f"hook script not executable and chmod failed at {cmd}: {e}")
            action = add_hook(settings, event, cmd)
        else:
            action = remove_hook(settings, event, cmd)
        log.append(f"  {name:22s} → {action}")
    return settings, log


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--hook", choices=("session-start", "user-prompt-submit",
                                       "pre-tool-use", "card-before-edit", "stop",
                                       "both", "all", "live"),
                    default="session-start",
                    help="which hook(s) to install. 'all' and 'live' are the SAME "
                         "complete set (RECOMMENDED) = session-start (digest + daily "
                         "auto-open) + user-prompt-submit (per-turn LIVE nudge #360) + "
                         "pre-tool-use (flash + #102 auto-link) + card-before-edit "
                         "(#75 non-blocking 'declare a card before editing' WARN) + stop "
                         "(advisory sign-off backstop #279/#359/#592 — writes a "
                         "recon note for next session; set BOARD_STEWARD_STRICT=1 "
                         "for same-turn blocking). 'both' = session-start + "
                         "user-prompt-submit only (legacy 2-hook alias). Or pass a single "
                         "hook name.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true", help="remove ALL board-steward hooks")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    if args.status:
        return cmd_status()

    path = claude_settings_path()
    settings = load_settings(path)

    if args.uninstall:
        selected: set[str] = set()
    elif args.hook == "both":
        # legacy alias preserved: SessionStart + UserPromptSubmit
        selected = {"session-start", "user-prompt-submit"}
    elif args.hook in ("all", "live"):
        # 'all' and 'live' are ONE canonical complete set — no all-vs-live
        # ambiguity (#369). Genuinely ALL four hooks, the full experience a
        # fresh install (and we) run: SessionStart (digest + daily auto-open) +
        # UserPromptSubmit (#360 per-turn LIVE nudge) + PreToolUse (flash on edit
        # + #102 auto-link) + Stop (#359 blocking sign-off backstop). install.sh
        # wires this so a download reflects everything, not a subset.
        selected = {"session-start", "user-prompt-submit", "pre-tool-use",
                    "card-before-edit", "stop"}
    else:
        selected = {args.hook}

    new_settings, log = apply_selection(settings, selected, dry_run=args.dry_run)
    any_change = any(("installed" in line and "already" not in line) or "uninstalled" in line
                     for line in log)

    if args.dry_run:
        print(f"DRY-RUN: target selection = {sorted(selected) or 'NONE (uninstall)'}")
        for line in log:
            print(line)
        print("--- would-be settings.json ---")
        print(json.dumps(new_settings, indent=2))
        return 0

    if not any_change:
        print(f"no-op (state already matches request) — {path}")
        for line in log:
            print(line)
        return 0

    if not args.no_backup and path.exists():
        bk = backup(path)
        if bk:
            print(f"backup: {bk}")
    atomic_write(path, new_settings)
    print(f"updated → {path}")
    for line in log:
        print(line)
    print("(restart any open Claude Code session to pick up the change)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
