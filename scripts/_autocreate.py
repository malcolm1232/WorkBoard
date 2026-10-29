#!/usr/bin/env python3
"""#960 - decide whether a Claude Code session in a board-less folder should
get a NEW WorkBoard automatically.

Used by hook_session_start.sh (subcommand `decide`), by autocreate_board.sh
(`any-tab`) and by `card.py autocreate ...` (the settings helpers). Pure
stdlib, one optional `git` call, so it stays well under the SessionStart
budget.

Rules (see README "Automatic boards"):

  1. Project root = the git top-level when the cwd is inside a repo, else the
     cwd. A linked worktree (incl. .claude/worktrees/* agent worktrees) maps to
     its MAIN repo (via `git rev-parse --git-common-dir`): the main repo's board
     is used, and a board is never created inside the worktree itself.
     A repo whose top-level is a container (a dotfiles repo at $HOME, a repo
     spanning ~/Desktop) does not make the container the project: the root is
     then the first non-container folder between it and the cwd.
  2. An existing board at or above the root (or the main repo) always wins.
  3. Never create for container folders themselves (/, /Users, /home, $HOME,
     ~/Desktop, ~/Documents, ~/Downloads, ..., iCloud / OneDrive / Dropbox /
     Google Drive roots, /Volumes/<disk>), nor for anything under system or
     scratch trees (/tmp, /private, /var, /opt, /usr, /System, /Library,
     /Applications, $TMPDIR, Claude scratchpads, ~/Library app data) or under a
     .claude / .git / node_modules / .venv / venv / site-packages folder.
     Under ~/Downloads only a git repo counts. Any other sub-folder of a
     container (e.g. ~/Desktop/my-idea, empty, no git) IS a new project.
  4. Only for sessions a person is attending. Claude Code sets
     CLAUDE_CODE_SESSION_ATTENDED in every hook's environment ("1" for an
     interactive terminal / IDE / desktop session, "0" for `claude -p`, SDK and
     background runs). Sub-agents never fire SessionStart at all (they fire
     SubagentStart). Older Claude Code without that variable: an
     `sdk-*` CLAUDE_CODE_ENTRYPOINT or a bg/daemon CLAUDE_CODE_SESSION_KIND
     means headless.
  5. ~/.board-steward/config.json: {"auto_create": "auto"|"ask"|"off",
     "never": [paths]}. BOARD_NO_AUTO_BOOTSTRAP=1 turns creation off too.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MODES = ("auto", "ask", "off")
DEFAULT_MODE = "auto"

# Folders that hold projects but are never one themselves (relative to $HOME).
_HOME_CONTAINERS = (
    "Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music",
    "Public", "Library", "Applications", "Videos", "Templates",
    "Dropbox", "OneDrive", "Google Drive", "Google Drive/My Drive",
    "iCloud Drive", "Library/Mobile Documents",
    "Library/Mobile Documents/com~apple~CloudDocs",
    "Library/CloudStorage",
)
# Absolute trees where nothing is ever a user project (outside $HOME).
_SYSTEM_TREES = (
    "/tmp", "/private", "/var", "/opt", "/usr", "/System", "/Library",
    "/Applications", "/bin", "/sbin", "/etc", "/dev", "/proc", "/sys",
    "/snap", "/nix", "/cores",
)
# Folder names that mark tool state, never a project, at any depth.
_DENY_COMPONENTS = {".claude", ".git", "node_modules", ".venv", "venv",
                    "site-packages", "__pycache__", ".Trash"}


# ── paths ────────────────────────────────────────────────────────────────────

def _real(p) -> Path:
    try:
        return Path(os.path.realpath(os.path.expanduser(str(p))))
    except Exception:
        return Path(str(p))


def _under(p: Path, base: Path) -> bool:
    """True if p is base or inside it."""
    return p == base or base in p.parents


def config_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".board-steward" / "config.json"


def load_config(home: Path | None = None) -> dict:
    """Read the settings file. Missing / corrupt / wrong types fall back to the
    defaults, so a hand-edited file can never break session start."""
    cfg = {"auto_create": DEFAULT_MODE, "never": []}
    try:
        d = json.loads(config_path(home).read_text())
    except Exception:
        return cfg
    if isinstance(d, dict):
        m = d.get("auto_create")
        if m in MODES:
            cfg["auto_create"] = m
        nv = d.get("never")
        if isinstance(nv, list):
            cfg["never"] = [str(x) for x in nv if isinstance(x, str) and x.strip()]
        for k, v in d.items():          # keep unknown keys on round-trip
            cfg.setdefault(k, v)
    return cfg


def save_config(cfg: dict, home: Path | None = None) -> Path:
    p = config_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, p)
    return p


# ── session classification ──────────────────────────────────────────────────

def session_kind(env: dict, payload: dict) -> tuple[bool, str]:
    """(is_human, reason). See module docstring rule 4 for the evidence."""
    att = env.get("CLAUDE_CODE_SESSION_ATTENDED")
    if att is not None and att != "":
        return (att == "1"), f"CLAUDE_CODE_SESSION_ATTENDED={att}"
    kind = env.get("CLAUDE_CODE_SESSION_KIND", "")
    if kind in ("bg", "daemon", "daemon-worker"):
        return False, f"CLAUDE_CODE_SESSION_KIND={kind}"
    ep = env.get("CLAUDE_CODE_ENTRYPOINT", "")
    if ep.startswith("sdk"):
        return False, f"CLAUDE_CODE_ENTRYPOINT={ep}"
    if payload.get("agent_id"):
        return False, "payload agent_id"
    return True, f"CLAUDE_CODE_ENTRYPOINT={ep or 'unset'}"


# ── git ──────────────────────────────────────────────────────────────────────

def git_info(cwd: Path) -> tuple[Path | None, Path | None]:
    """(top_level, main_repo_top_level_if_linked_worktree). One git call."""
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse",
             "--show-toplevel", "--git-common-dir", "--git-dir"],
            capture_output=True, text=True, timeout=3)
    except Exception:
        return None, None
    if r.returncode != 0:
        return None, None
    lines = [x for x in r.stdout.splitlines() if x.strip()]
    if len(lines) < 3:
        return None, None
    # --show-toplevel is absolute; the other two may be relative to cwd (no
    # --path-format=absolute: that needs git 2.31+).
    top, common, gitdir = (_real(x if os.path.isabs(x) else os.path.join(str(cwd), x))
                           for x in lines[:3])
    main = None
    if common != gitdir and common.name == ".git":
        main = common.parent        # linked worktree → its main checkout
    return top, main


def find_board_up(start: Path) -> Path | None:
    d = start
    while True:
        bj = d / "board" / "board.json"
        if bj.is_file():
            return bj
        if d.parent == d:
            return None
        d = d.parent


# ── classification of a candidate root ──────────────────────────────────────

def _containers(home: Path) -> set[Path]:
    c = {Path("/"), Path("/Users"), Path("/home"), Path("/Volumes"), Path("/mnt"),
         Path("/media"), home}
    for rel in _HOME_CONTAINERS:
        c.add(home / rel)
    # ~/OneDrive - Org, ~/Library/CloudStorage/<Provider>[/My Drive]
    try:
        for child in home.iterdir():
            if child.name.startswith("OneDrive") or child.name.startswith("Google Drive"):
                c.add(_real(child))
    except OSError:
        pass
    cs = home / "Library" / "CloudStorage"
    try:
        for child in cs.iterdir():
            c.add(child)
            c.add(child / "My Drive")
    except OSError:
        pass
    return c


def is_container(p: Path, home: Path) -> bool:
    if p in _containers(home):
        return True
    # /Volumes/<disk>, /mnt/<x>, /media/<user>[/<disk>]
    parts = p.parts
    if len(parts) == 3 and parts[1] in ("Volumes", "mnt"):
        return True
    if len(parts) in (3, 4) and parts[1] == "media":
        return True
    return False


def deny_reason(root: Path, home: Path, env: dict, payload: dict,
                is_git: bool) -> str | None:
    """Why `root` must never get an automatic board, or None if it may."""
    if is_container(root, home):
        return f"container folder ({root})"
    in_home = home not in (Path("/"),) and home in root.parents
    for comp in root.parts:
        if comp in _DENY_COMPONENTS:
            return f"tool folder ({comp})"
    sp = payload.get("scratchpad_dir")
    if sp and _under(root, _real(sp)):
        return "Claude scratchpad"
    tmp = env.get("TMPDIR")
    if tmp and _under(root, _real(tmp)):
        return "temporary folder ($TMPDIR)"
    if not in_home:
        for t in _SYSTEM_TREES:
            if _under(root, _real(t)) or _under(root, Path(t)):
                return f"system folder ({t})"
    else:
        lib = home / "Library"
        if _under(root, lib):
            cloud = (lib / "Mobile Documents" / "com~apple~CloudDocs",
                     lib / "CloudStorage")
            if not any(c in root.parents for c in cloud):
                return "app data (~/Library)"
        if _under(root, home / "Downloads") and not is_git:
            return "~/Downloads folder that is not a git repo"
        rel = root.relative_to(home).parts
        if rel and rel[0].startswith(".") and not is_git:
            return f"hidden folder (~/{rel[0]})"
    return None


def project_root(cwd: Path, home: Path) -> tuple[Path, bool, Path | None]:
    """(root, root_is_git_top_level, main_repo_if_worktree)."""
    top, main = git_info(cwd)
    if main is not None:
        return main, True, main
    if top is None:
        return cwd, False, None
    if is_container(top, home) and top != cwd:
        # Repo spans a container (dotfiles repo at $HOME …): the project is the
        # first non-container folder between it and the cwd.
        rel = cwd.relative_to(top).parts if top in cwd.parents else ()
        d = top
        for comp in rel:
            d = d / comp
            if not is_container(d, home):
                return d, False, None
        return cwd, False, None
    return top, True, None


def _never_match(root: Path, never: list[str]) -> str | None:
    for n in never:
        if _under(root, _real(n)):
            return n
    return None


def decide(cwd, env: dict | None = None, payload: dict | None = None,
           home=None) -> dict:
    """Return {"action": use|create|ask|skip, "root", "board", "reason"}."""
    env = dict(os.environ) if env is None else env
    payload = payload or {}
    home = _real(home or env.get("HOME") or Path.home())
    cwd = _real(cwd)
    out = {"action": "skip", "root": "", "board": "", "reason": ""}

    root, is_git, main = project_root(cwd, home)
    out["root"] = str(root)
    # Rule 2 (+ worktree half of rule 1): an existing board always wins.
    # Worktree → the MAIN repo's board first (a worktree may carry its own
    # tracked copy of board/, which must not become a second board); else the
    # nearest board at or above the cwd.
    bj = find_board_up(main) if main is not None else None
    bj = bj or find_board_up(cwd)
    if bj is not None:
        out.update(action="use", board=str(bj),
                   reason="main repo board" if main is not None else "existing board")
        return out

    human, why = session_kind(env, payload)
    if not human:
        out["reason"] = f"not a person's session ({why})"
        return out
    if env.get("BOARD_NO_AUTO_BOOTSTRAP", "0") == "1":
        out["reason"] = "BOARD_NO_AUTO_BOOTSTRAP=1"
        return out
    cfg = load_config(home)
    if cfg["auto_create"] == "off":
        out["reason"] = "auto_create is off"
        return out
    nm = _never_match(root, cfg["never"])
    if nm:
        out["reason"] = f"in the never list ({nm})"
        return out
    dr = deny_reason(root, home, env, payload, is_git)
    if dr:
        out["reason"] = dr
        return out
    out["action"] = "ask" if cfg["auto_create"] == "ask" else "create"
    out["reason"] = f"new project ({why})"
    return out


# ── is any WorkBoard tab open? ──────────────────────────────────────────────

def any_board_tab(exclude: int | None = None) -> bool:
    """True if a browser tab already shows ANY WorkBoard (any board, any port).

    Uses the #959 auto-open gate's own read-only Chrome probe (_autoopen.py),
    so "is a board tab open" means the same thing everywhere. When Chrome's
    state cannot be read (no AppleScript, denied, timed out), a live board with
    a connected or recently connected viewer counts as an open tab."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _autoopen as ao
    except Exception:
        return False
    known = ao._board_ports()
    ch = ao.probe_chrome(known)
    tabs = [t for t in ch.get("board_tabs", []) if exclude is None or t[0] != exclude]
    if tabs:
        return True
    if ch.get("state") != "unknown":
        return False
    return any(ao._viewer_recent(ao._health(p, 0.6))
               for p in sorted(known) if p != exclude)


# ── hook text ────────────────────────────────────────────────────────────────

def notice_marker(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".board-steward" / ".autocreate-notice-shown"


def create_notice(root: str, port: int, card_py: str, first: bool) -> str:
    name = Path(root).name
    url = f"http://127.0.0.1:{port}"
    if first:
        return (
            f"WorkBoard: this folder had no board, so a new one is being created for "
            f"'{name}' at {root}/board ({url}). This is the first automatic board: tell "
            f"the user in ONE line what was created and where, and that they can turn "
            f"automatic boards off with `python3 {card_py} autocreate mode off` (or "
            f"`mode ask` to be asked first) or exclude this folder with "
            f"`python3 {card_py} autocreate never \"{root}\"`."
        )
    return (f"WorkBoard: new board for '{name}' at {url} (created automatically). "
            f"Mention it to the user in one short line.")


def ask_notice(root: str, create_sh: str, card_py: str) -> str:
    """create_sh is autocreate_board.sh: a "yes" goes through the same
    no-extra-tab path as an automatic creation."""
    name = Path(root).name
    return (
        f"WorkBoard: this folder ('{name}', {root}) has no board and automatic boards "
        f"are set to ask. Ask the user in ONE line: \"Create a WorkBoard for {name}? "
        f"(yes / no / never for this folder)\". yes → run `bash \"{create_sh}\" "
        f"\"{root}\"`; never → run `python3 {card_py} autocreate never \"{root}\"`; "
        f"no → do nothing."
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    if not argv:
        print("usage: _autocreate.py decide <cwd> | any-tab [--exclude PORT]", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "decide":
        cwd = argv[1] if len(argv) > 1 else os.getcwd()
        try:
            payload = json.loads(os.environ.get("BOARD_HOOK_PAYLOAD") or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        d = decide(cwd, payload=payload)
        # Tab-separated so the shell can `read` it without a JSON parser.
        # The only field that can be empty (board) goes LAST: bash `read` with
        # IFS=tab collapses consecutive tabs, so an empty middle field would
        # shift every later one.
        print("\t".join([d["action"], d["root"], d["reason"], d["board"]]))
        return 0
    if cmd == "any-tab":
        ex = None
        if "--exclude" in argv:
            try:
                ex = int(argv[argv.index("--exclude") + 1])
            except (IndexError, ValueError):
                ex = None
        return 0 if any_board_tab(exclude=ex) else 1
    if cmd == "notice":
        # notice <root> <port> <card_py>: prints the create line and records
        # that the first-time notice has been shown.
        root, port, card_py = argv[1], int(argv[2]), argv[3]
        m = notice_marker()
        first = not m.exists()
        print(create_notice(root, port, card_py, first))
        if first:
            try:
                m.parent.mkdir(parents=True, exist_ok=True)
                m.write_text("")
            except OSError:
                pass
        return 0
    if cmd == "ask":
        print(ask_notice(argv[1], argv[2], argv[3]))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
