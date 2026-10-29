#!/usr/bin/env python3
"""#960 E2E - the real SessionStart hook creates a board for a new project.

Runs scripts/hook_session_start.sh exactly as Claude Code does (stdin = the
SessionStart JSON payload, cwd = the session folder, CLAUDE_CODE_* env as
measured from a real interactive / `claude -p` start) inside a throwaway HOME.
Real serve.py servers, real bootstrap_project.sh, real board_autoopen.sh, real
HTTP and SSE. Only the outside world is stubbed with PATH shims: `open` (logs
each tab it would open), `osascript` / `pgrep` (a fake Chrome whose tabs are a
file), `launchctl` (no real login items) and `claude` (no model calls).

Ports: the fake HOME's port map is pre-seeded so new boards land on free ports
above any real board server on this machine.

Run: python3 dev/test_960_hook_e2e.py  → exit 0 = green.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "scripts" / "hook_session_start.sh"

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'ok ' if cond else 'FAIL'} {msg}")
    if not cond:
        _fails += 1


def _free(p):
    with socket.socket() as s:
        s.settimeout(0.1)
        return s.connect_ex(("127.0.0.1", p)) != 0


def wait_for(fn, timeout=20.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def health(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as r:
        return json.loads(r.read())


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})


class Sandbox:
    def __init__(self, base: Path):
        self.base = Path(os.path.realpath(base))
        self.home = self.base / "home"
        for d in ("Desktop", "Downloads", "Documents"):
            (self.home / d).mkdir(parents=True)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.tmp = self.base / "tmpx"
        self.tmp.mkdir()
        self.open_log = self.base / "open.log"
        self.tabs = self.base / "chrome_tabs.txt"     # one URL per line
        self.tabs.write_text("")
        self._shim("open", f'echo "$@" >> "{self.open_log}"\n')
        # Fake Chrome. _autocreate.any_board_tab asks for every tab URL;
        # board_autoopen.sh asks "does a tab contain 127.0.0.1:<port>".
        self._shim("osascript", f'''in="$(cat)"
case "$in" in
  *"set out to"*) cat "{self.tabs}" ;;
  *) p="$(printf '%s' "$in" | grep -o '127.0.0.1:[0-9]*' | head -n 1)"
     if [ -n "$p" ] && grep -q "$p" "{self.tabs}"; then echo yes; else echo no; fi ;;
esac
''')
        self._shim("pgrep", "exit 0\n")
        self._shim("launchctl", f'echo "$@" >> "{self.base}/launchctl.log"\n')
        self._shim("claude", "exit 1\n")
        # Seed the port map so our boards bind above any real board server.
        self.port_lo = next(p for p in range(7931, 7990) if all(_free(p + i) for i in range(8)))
        seed = {}
        for p in range(7891, self.port_lo):
            d = self.base / "seed" / str(p)
            d.mkdir(parents=True)
            seed[str(d)] = p
        st = self.home / ".board-steward"
        st.mkdir()
        (st / "port-assignments.json").write_text(json.dumps(seed))

    def _shim(self, name, body):
        p = self.bin / name
        p.write_text("#!/usr/bin/env bash\n" + body)
        p.chmod(0o755)

    def env(self, attended=True, **extra):
        e = {k: v for k, v in os.environ.items()
             if not (k.startswith("CLAUDE") or k.startswith("BOARD_") or k == "AI_AGENT")}
        e.update(HOME=str(self.home), PATH=f"{self.bin}:{os.environ['PATH']}",
                 TMPDIR=str(self.tmp), BOARD_NO_RECON="1")
        if attended:   # measured from a real interactive start (see report)
            e.update(CLAUDE_CODE_ENTRYPOINT="cli", CLAUDE_CODE_SESSION_ATTENDED="1")
        else:          # measured from a real `claude -p` start
            e.update(CLAUDE_CODE_ENTRYPOINT="sdk-cli", CLAUDE_CODE_SESSION_ATTENDED="0")
        e.update(CLAUDECODE="1", CLAUDE_CODE_CHILD_SESSION="1")
        e.update({k: v for k, v in extra.items() if v is not None})
        return e

    def hook(self, cwd: Path, attended=True, sid="sess-1", **extra):
        cwd.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": sid, "transcript_path": str(self.home / "t.jsonl"),
                   "cwd": str(cwd), "hook_event_name": "SessionStart", "source": "startup"}
        if attended:
            payload["model"] = "claude-opus-5-5"
        t0 = time.time()
        r = subprocess.run(["bash", str(HOOK)], input=json.dumps(payload), cwd=str(cwd),
                           env=self.env(attended, **extra), capture_output=True, text=True,
                           timeout=60)
        if os.environ.get("E2E_VERBOSE") == "1":
            print("    ---- hook output ----\n" + "\n".join("    | " + l for l in r.stdout.splitlines()))
        return r.stdout, time.time() - t0

    def opens(self):
        return self.open_log.read_text().splitlines() if self.open_log.exists() else []

    def port_of(self, root: Path):
        a = json.loads((self.home / ".board-steward" / "port-assignments.json").read_text())
        return a.get(str(root / "board"))

    def cleanup(self):
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        for line in out.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if str(self.base) in cmd and int(pid) != os.getpid():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except Exception:
                    pass


class SSE:
    """Collect event names + data from a board's /events stream."""
    def __init__(self, port):
        self.events = []
        self._r = urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=30)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        name = None
        try:
            for raw in self._r:
                line = raw.decode(errors="replace").rstrip("\n")
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:") and name:
                    try:
                        self.events.append((name, json.loads(line[5:].strip())))
                    except Exception:
                        self.events.append((name, line[5:]))
                    name = None
        except Exception:
            pass


def main():
    with tempfile.TemporaryDirectory() as td:
        sb = Sandbox(Path(td))
        H = sb.home
        try:
            print("0. fresh user (no board yet) outside a project, and headless")
            out, _ = sb.hook(H / "Desktop", sid="sess-0a")
            check("outside a project folder" in out and not (H / "Desktop" / "board").exists(),
                  "~/Desktop itself: first-board picker / hint, no board")
            hp = H / "Desktop" / "headless-first"
            out, _ = sb.hook(hp, attended=False, sid="sess-0b")
            time.sleep(1.5)
            check(out.strip() == "" and not (hp / "board").exists(),
                  "claude -p in a new folder before any board: silent, no board, no picker")
            check(not (H / ".board-steward" / ".onboarded").exists(), "still not onboarded")

            print("A. first new project of a fresh user, no browser tab open")
            a = H / "Desktop" / "my-idea"
            out, dt = sb.hook(a)
            check(dt < 2.5, f"hook returned in {dt:.2f}s (bootstrap is backgrounded)")
            check("first automatic board" in out and "autocreate mode off" in out
                  and "autocreate never" in out, "first-time notice: what, where, off, exclude")
            check(str(a / "board" / "board.json") in out, "notice names the new board path")
            pa = sb.port_of(a)
            check(pa is not None and pa >= sb.port_lo, f"sticky port assigned ({pa})")
            check(wait_for(lambda: (a / "board" / "board.json").is_file()) is not None,
                  "board.json created in ~/Desktop/my-idea/board")
            check(wait_for(lambda: health(pa).get("ok")) is not None, "its server is live")
            check(wait_for(lambda: len(sb.opens()) == 1, 20) is not None,
                  "exactly one tab opened (no WorkBoard tab existed)")
            check(any(f"127.0.0.1:{pa}" in o for o in sb.opens()), "the tab is the new board")
            check((H / ".board-steward" / ".onboarded").exists(), "user marked onboarded")
            check((H / ".board-steward" / ".autocreate-notice-shown").exists(), "first notice recorded")

            # The user now has that board open in Chrome.
            sb.tabs.write_text(f"http://127.0.0.1:{pa}/?sid=sess-1\n")
            sse = SSE(pa)
            time.sleep(0.5)

            print("B. second new project while a WorkBoard tab is open")
            b = H / "Desktop" / "second"
            out, dt = sb.hook(b, sid="sess-2")
            check(dt < 2.5, f"hook returned in {dt:.2f}s")
            check("first automatic board" not in out and "new board for 'second'" in out,
                  "later notice is one short line")
            check(wait_for(lambda: (b / "board" / "board.json").is_file()) is not None,
                  "second board created")
            pb = sb.port_of(b)
            check(pb is not None and pb != pa and wait_for(lambda: health(pb).get("ok")) is not None,
                  f"second board live on its own port ({pb})")
            got = wait_for(lambda: [d for n, d in sse.events if n == "boards-changed"], 15)
            check(bool(got) and got[0].get("path") == str(b / "board") and got[0].get("port") == pb,
                  "open tab got a boards-changed SSE event naming the new board")
            with urllib.request.urlopen(f"http://127.0.0.1:{pa}/boards", timeout=3) as r:
                listed = [x["path"] for x in json.loads(r.read())["boards"]]
            check(str(b / "board") in listed, "the open tab's switcher (/boards) lists the new board")
            time.sleep(3)
            check(len(sb.opens()) == 1, "no second tab opened")

            print("C. headless claude -p in a new folder")
            c = H / "Desktop" / "ci-run"
            out, dt = sb.hook(c, attended=False, sid="sess-3")
            time.sleep(2)
            check(not (c / "board").exists(), "no board created for a headless session")
            check("automatic" not in out and "Board: " in out,
                  "keeps today's fallback (last-active board digest)")

            print("D. agent worktree of a repo that has a board")
            m = H / "Desktop" / "mainrepo"
            m.mkdir()
            git("init", "-q", "-b", "main", cwd=m)
            (m / "README").write_text("x\n")
            git("add", "README", cwd=m)
            git("commit", "-q", "-m", "init", cwd=m)
            (m / "board").mkdir()
            (m / "board" / "board.json").write_text(json.dumps({"title": "mainrepo", "cards": [], "columns": []}))
            wt = m / ".claude" / "worktrees" / "agent-x"
            git("worktree", "add", "-q", "-b", "agent-x", str(wt), cwd=m)
            out, dt = sb.hook(wt, sid="sess-4")
            check(f"Board: {m / 'board' / 'board.json'}" in out, "session uses the MAIN repo's board")
            check(not (wt / "board").exists(), "no board created inside the worktree")

            print("E. ~/Downloads folder that is not a git repo")
            e = H / "Downloads" / "unzipped-thing"
            out, dt = sb.hook(e, sid="sess-5")
            time.sleep(1.5)
            check(not (e / "board").exists() and "automatic" not in out, "no board in ~/Downloads/unzipped-thing")

            print("F. ~/Desktop itself")
            out, dt = sb.hook(H / "Desktop", sid="sess-6")
            time.sleep(1)
            check(not (H / "Desktop" / "board").exists(), "no board in ~/Desktop")

            print("G. ask mode")
            (H / ".board-steward" / "config.json").write_text(json.dumps({"auto_create": "ask"}))
            g = H / "Desktop" / "maybe"
            out, dt = sb.hook(g, sid="sess-7")
            time.sleep(1.5)
            check("yes / no / never" in out and not (g / "board").exists(),
                  "Claude is handed one question; nothing created")

            print("H. never-list and off")
            (H / ".board-steward" / "config.json").write_text(json.dumps(
                {"auto_create": "auto", "never": [str(H / "Desktop" / "private")]}))
            p = H / "Desktop" / "private" / "x"
            sb.hook(p, sid="sess-8")
            (H / ".board-steward" / "config.json").write_text(json.dumps({"auto_create": "off"}))
            o = H / "Desktop" / "offproj"
            sb.hook(o, sid="sess-9")
            time.sleep(1.5)
            check(not (p / "board").exists() and not (H / "Desktop" / "private" / "board").exists(),
                  "never-listed folder gets no board")
            check(not (o / "board").exists(), "auto_create off → no board")

            print("I. BOARD_NO_AUTO_BOOTSTRAP=1")
            (H / ".board-steward" / "config.json").unlink()
            n = H / "Desktop" / "optout"
            out, dt = sb.hook(n, sid="sess-10", BOARD_NO_AUTO_BOOTSTRAP="1")
            time.sleep(1.5)
            check(not (n / "board").exists() and out.strip() == "", "opt-out env: silent, no board")
            check(not (sb.base / "launchctl.log").exists()
                  or all(str(sb.home) in l for l in (sb.base / "launchctl.log").read_text().splitlines()),
                  "autostart only ever touched the sandbox HOME")
        finally:
            sb.cleanup()
    print()
    print("PASS" if _fails == 0 else f"{_fails} FAILED")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
