#!/usr/bin/env python3
"""#960 — which folders get an automatic board (scripts/_autocreate.py).

Every rule of the decision is exercised against a throwaway HOME: container
folders, system / scratch / tool trees, a Desktop sub-folder, Downloads with and
without git, git sub-folders, worktree → main repo mapping, an existing parent
board, person vs headless sessions, the settings modes, the never-list, and the
first-time vs later notice. The card.py `autocreate` commands run as real
subprocesses with that HOME.

Run: python3 dev/test_960_autocreate.py  → exit 0 = green.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import _autocreate as ac  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'ok ' if cond else 'FAIL'} {msg}")
    if not cond:
        _fails += 1


HUMAN = {"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})


def make_repo(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=p)
    (p / "README").write_text("x\n")
    git("add", "README", cwd=p)
    git("commit", "-q", "-m", "init", cwd=p)
    return p


def make_board(root: Path) -> Path:
    (root / "board").mkdir(parents=True, exist_ok=True)
    bj = root / "board" / "board.json"
    bj.write_text(json.dumps({"title": root.name, "cards": [], "columns": []}))
    return bj


class Home:
    def __init__(self, base: Path):
        self.home = ac._real(base / "home")
        self.home.mkdir()
        for d in ("Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music",
                  "Public", "Library", "Applications"):
            (self.home / d).mkdir()
        (self.home / "Library" / "Mobile Documents" / "com~apple~CloudDocs").mkdir(parents=True)
        (self.home / "Library" / "CloudStorage" / "OneDrive-Acme").mkdir(parents=True)
        (self.home / "Library" / "CloudStorage" / "GoogleDrive-a@b.c" / "My Drive").mkdir(parents=True)
        (self.home / "Dropbox").mkdir()
        (self.home / "OneDrive - Acme").mkdir()

    def env(self, **extra):
        e = {"HOME": str(self.home), **HUMAN}
        e.update(extra)
        return {k: v for k, v in e.items() if v is not None}

    def decide(self, cwd, payload=None, **env):
        Path(cwd).mkdir(parents=True, exist_ok=True)
        return ac.decide(cwd, env=self.env(**env), payload=payload or {}, home=self.home)

    def config(self, cfg):
        p = self.home / ".board-steward" / "config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg))


def main():
    with tempfile.TemporaryDirectory() as td:
        h = Home(Path(td))
        H = h.home

        print("new projects (create)")
        d = h.decide(H / "Desktop" / "my-idea")
        check(d["action"] == "create" and d["root"] == str(H / "Desktop" / "my-idea"),
              "empty ~/Desktop/my-idea (no git) → create there")
        d = h.decide(H / "Documents" / "notes-app")
        check(d["action"] == "create", "~/Documents/notes-app → create")
        d = h.decide(H / "code" / "thing")
        check(d["action"] == "create", "~/code/thing → create")
        repo = make_repo(H / "Desktop" / "repo")
        d = h.decide(repo / "src" / "deep")
        check(d["action"] == "create" and d["root"] == str(repo),
              "sub-folder of a git repo → root is the git top-level")
        icloud = H / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "proj"
        d = h.decide(icloud)
        check(d["action"] == "create", "a folder inside iCloud Drive → create")
        d = h.decide(H / "Library" / "CloudStorage" / "OneDrive-Acme" / "proj")
        check(d["action"] == "create", "a folder inside OneDrive (CloudStorage) → create")

        print("container folders (never)")
        for c in [H, H / "Desktop", H / "Documents", H / "Downloads", H / "Pictures",
                  H / "Movies", H / "Music", H / "Public", H / "Library", H / "Applications",
                  H / "Library" / "Mobile Documents",
                  H / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
                  H / "Library" / "CloudStorage" / "OneDrive-Acme",
                  H / "Library" / "CloudStorage" / "GoogleDrive-a@b.c" / "My Drive",
                  H / "Dropbox", H / "OneDrive - Acme"]:
            d = h.decide(c)
            check(d["action"] == "skip" and d["reason"].startswith("container"),
                  f"{c.relative_to(H.parent)} → skip ({d['reason']})")
        for p in ["/", "/Users", "/home", "/Volumes", "/Volumes/External", "/mnt/data"]:
            r = ac.deny_reason(Path(p), H, {}, {}, False)
            check(r is not None and r.startswith("container"), f"{p} → container")
        r = ac.deny_reason(Path("/Volumes/External/proj"), H, {}, {}, False)
        check(r is None, "/Volumes/External/proj (a project on an external disk) → allowed")

        print("system, scratch and tool folders (never)")
        for p in ["/tmp/x", "/private/tmp/x", "/var/x", "/opt/x", "/usr/local/src/x",
                  "/System/x", "/Library/x", "/Applications/Foo.app/x", "/etc/x"]:
            r = ac.deny_reason(Path(p), H, {}, {}, False)
            check(r is not None and r.startswith("system"), f"{p} → {r}")
        d = h.decide(H / "work" / "tmpdir" / "x", TMPDIR=str(H / "work" / "tmpdir"))
        check(d["action"] == "skip" and "TMPDIR" in d["reason"], "inside $TMPDIR → skip")
        sp = H / "work" / "scratch"
        d = h.decide(sp / "x", payload={"scratchpad_dir": str(sp)})
        check(d["action"] == "skip" and "scratchpad" in d["reason"], "Claude scratchpad → skip")
        for comp in [".claude", ".git", "node_modules", ".venv", "venv", "site-packages"]:
            d = h.decide(H / "code" / "app" / comp / "inner")
            check(d["action"] == "skip" and "tool folder" in d["reason"],
                  f"under a {comp} folder → skip")
        d = h.decide(H / "Library" / "Application Support" / "Foo")
        check(d["action"] == "skip" and "Library" in d["reason"], "~/Library app data → skip")
        d = h.decide(H / ".config" / "nvim")
        check(d["action"] == "skip" and "hidden" in d["reason"], "~/.config/nvim (no git) → skip")
        dot = make_repo(H / ".dotfiles")
        d = h.decide(dot)
        check(d["action"] == "create", "~/.dotfiles that is a git repo → create")

        print("Downloads")
        d = h.decide(H / "Downloads" / "unzipped")
        check(d["action"] == "skip" and "Downloads" in d["reason"],
              "~/Downloads/unzipped (no git) → skip")
        dl = make_repo(H / "Downloads" / "cloned")
        d = h.decide(dl / "src")
        check(d["action"] == "create" and d["root"] == str(dl),
              "~/Downloads/cloned (git) → create at the repo")

        print("repo spanning a container (dotfiles repo at $HOME)")
        with tempfile.TemporaryDirectory() as td2:
            h2 = Home(Path(td2))
            make_repo(h2.home)
            d = h2.decide(h2.home / "Desktop" / "idea" / "src")
            check(d["action"] == "create" and d["root"] == str(h2.home / "Desktop" / "idea"),
                  "git at $HOME, cwd ~/Desktop/idea/src → root ~/Desktop/idea")
            d = h2.decide(h2.home)
            check(d["action"] == "skip", "git at $HOME, cwd $HOME → skip")
            d = h2.decide(h2.home / "Downloads" / "junk")
            check(d["action"] == "skip", "git at $HOME, cwd ~/Downloads/junk → skip")

        print("existing boards win")
        parent = H / "Desktop" / "has-board"
        bj = make_board(parent)
        d = h.decide(parent / "sub" / "deeper")
        check(d["action"] == "use" and d["board"] == str(bj), "board in a parent → use it")
        d = h.decide(parent, CLAUDE_CODE_SESSION_ATTENDED="0")
        check(d["action"] == "use", "existing board is used by a headless session too")

        print("worktrees map to the main repo")
        main_repo = make_repo(H / "Desktop" / "mainrepo")
        wt_in = main_repo / ".claude" / "worktrees" / "agent-1"
        git("worktree", "add", "-q", "-b", "wt1", str(wt_in), cwd=main_repo)
        wt_out = H / "Desktop" / "mainrepo-feature"
        git("worktree", "add", "-q", "-b", "wt2", str(wt_out), cwd=main_repo)
        d = h.decide(wt_out)
        check(d["action"] == "create" and d["root"] == str(main_repo),
              "worktree, main repo without a board → root is the MAIN repo, not the worktree")
        d = h.decide(wt_in)
        check(d["action"] == "create" and d["root"] == str(main_repo),
              ".claude/worktrees/* agent worktree → root is the main repo")
        mbj = make_board(main_repo)
        d = h.decide(wt_out / "sub")
        check(d["action"] == "use" and d["board"] == str(mbj), "outside worktree → main repo board")
        d = h.decide(wt_in)
        check(d["action"] == "use" and d["board"] == str(mbj), "agent worktree → main repo board")
        make_board(wt_out)   # a tracked copy of board/ inside the worktree
        d = h.decide(wt_out)
        check(d["action"] == "use" and d["board"] == str(mbj),
              "worktree carrying its own board/ copy → still the main repo board")

        print("person vs headless sessions")
        tgt = H / "Desktop" / "sess"
        d = h.decide(tgt)
        check(d["action"] == "create", "CLAUDE_CODE_SESSION_ATTENDED=1 → create")
        d = h.decide(tgt, CLAUDE_CODE_SESSION_ATTENDED="0", CLAUDE_CODE_ENTRYPOINT="sdk-cli")
        check(d["action"] == "skip" and "not a person" in d["reason"], "claude -p (ATTENDED=0) → skip")
        d = h.decide(tgt, CLAUDE_CODE_SESSION_ATTENDED=None, CLAUDE_CODE_ENTRYPOINT="sdk-py")
        check(d["action"] == "skip", "older Claude Code, entrypoint sdk-py → skip")
        d = h.decide(tgt, CLAUDE_CODE_SESSION_ATTENDED=None, CLAUDE_CODE_ENTRYPOINT="cli",
                     CLAUDE_CODE_SESSION_KIND="bg")
        check(d["action"] == "skip", "background session kind → skip")
        d = h.decide(tgt, CLAUDE_CODE_SESSION_ATTENDED=None, CLAUDE_CODE_ENTRYPOINT="cli")
        check(d["action"] == "create", "older Claude Code, interactive cli → create")
        d = h.decide(tgt, CLAUDE_CODE_SESSION_ATTENDED=None, CLAUDE_CODE_ENTRYPOINT="claude-vscode")
        check(d["action"] == "create", "IDE entrypoint → create")
        d = h.decide(tgt, payload={"agent_id": "a1"}, CLAUDE_CODE_SESSION_ATTENDED=None)
        check(d["action"] == "skip", "payload carrying an agent_id → skip")

        print("settings")
        d = h.decide(tgt, BOARD_NO_AUTO_BOOTSTRAP="1")
        check(d["action"] == "skip" and "BOARD_NO_AUTO_BOOTSTRAP" in d["reason"],
              "BOARD_NO_AUTO_BOOTSTRAP=1 → skip")
        h.config({"auto_create": "off"})
        check(h.decide(tgt)["action"] == "skip", "mode off → skip")
        h.config({"auto_create": "ask"})
        check(h.decide(tgt)["action"] == "ask", "mode ask → ask")
        h.config({"auto_create": "auto", "never": [str(H / "Desktop" / "sess")]})
        d = h.decide(tgt / "inner")
        check(d["action"] == "skip" and "never" in d["reason"], "never-list folder (and below) → skip")
        check(h.decide(H / "Desktop" / "other")["action"] == "create", "a folder not in never → create")
        h.config("{not json")
        check(h.decide(tgt)["action"] == "create", "corrupt config.json → default auto")
        h.config({"auto_create": "sometimes", "never": "oops"})
        check(h.decide(tgt)["action"] == "create", "bad values in config.json → defaults")

        print("card.py autocreate commands")
        (H / ".board-steward" / "config.json").unlink()
        env = {**os.environ, "HOME": str(H)}

        def card(*a):
            return subprocess.run([sys.executable, str(SCRIPTS / "card.py"), "autocreate", *a],
                                  capture_output=True, text=True, env=env, cwd=str(H))
        r = card("status")
        check(r.returncode == 0 and "auto_create: auto" in r.stdout, "status shows the default mode")
        r = card("mode", "ask")
        check(r.returncode == 0 and ac.load_config(H)["auto_create"] == "ask", "mode ask is saved")
        r = card("mode", "bogus")
        check(r.returncode == 2 and ac.load_config(H)["auto_create"] == "ask", "a bad mode is refused")
        card("mode", "auto")
        r = card("never", str(H / "Desktop" / "secret"))
        check(r.returncode == 0 and str(H / "Desktop" / "secret") in ac.load_config(H)["never"],
              "never <path> is saved")
        check(h.decide(H / "Desktop" / "secret")["action"] == "skip", "…and that folder is now skipped")
        card("never", str(H / "Desktop" / "secret"))
        check(ac.load_config(H)["never"].count(str(H / "Desktop" / "secret")) == 1, "never is idempotent")
        r = card("allow", str(H / "Desktop" / "secret"))
        check(r.returncode == 0 and ac.load_config(H)["never"] == [], "allow <path> removes it")

        print("first-time vs later notice")
        def notice():
            return subprocess.run([sys.executable, str(SCRIPTS / "_autocreate.py"), "notice",
                                   str(H / "Desktop" / "a"), "7950", "/x/card.py"],
                                  capture_output=True, text=True, env=env).stdout
        n1 = notice()
        check("first automatic board" in n1 and "autocreate mode off" in n1
              and "autocreate never" in n1 and "7950" in n1,
              "first notice: what, where, how to turn off, how to exclude")
        n2 = notice()
        check("first automatic board" not in n2 and "7950" in n2 and len(n2) < len(n1) / 2,
              "later notice: one short line")
        a = ac.ask_notice(str(H / "Desktop" / "a"), "/x/bootstrap_project.sh", "/x/card.py")
        check("yes / no / never" in a and "bootstrap_project.sh" in a and "autocreate never" in a,
              "ask notice: one question with yes / no / never actions")

    print()
    print("PASS" if _fails == 0 else f"{_fails} FAILED")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
