#!/usr/bin/env python3
"""board_autoopen.sh — the passive (SessionStart) open fires at most ONCE PER DAY.

Bug: the policy was "open whenever no Chrome tab shows the board". Close the tab
(or quit Chrome) and the very next Claude session re-opened it — a new tab per
terminal, all day. The dedupe was correct; the policy was wrong.

Contract:
  - passive call (the hook): opens iff no tab shows the board AND this board has
    not been auto-opened today. Closing the tab is respected until tomorrow.
  - explicit call (BOARD_OPEN_EXPLICIT=1 — bootstrap / card.py board-new): the
    user asked for the board, so the daily gate is skipped; the tab-presence
    dedupe and the #122 burst cooldown still apply.
  - a tab that is already visible never opens a second one, in either mode.
  - BOARD_NO_AUTO_OPEN=1 still suppresses everything.

Harness: real board_autoopen.sh, real /health over HTTP, throwaway HOME; `open`,
`osascript` and `pgrep` are PATH shims so no browser is touched. The `open` shim
appends its argv to a log — the count of lines IS the number of tabs opened.

Run: python3 dev/test_autoopen_once_per_day.py  → exit 0 = green.
"""
from __future__ import annotations
import http.server, os, subprocess, sys, tempfile, threading, time
from datetime import date, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "board_autoopen.sh"
COOLDOWN = 12  # seconds — mirrors board_autoopen.sh

_fails = 0
def check(cond, msg):
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond: _fails += 1


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true, "sseClients": 0, "lastSseConnectMs": 0, "nowMs": 1}'
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass


class Rig:
    """One throwaway HOME + shim dir + live /health port."""
    def __init__(self, tmp: Path):
        self.home = tmp / "home"; self.home.mkdir()
        self.bin = tmp / "bin"; self.bin.mkdir()
        self.open_log = tmp / "open.log"
        self.tab_flag = tmp / "tab_present"      # exists → osascript says "yes"
        self._shim("open", f'echo "$@" >> "{self.open_log}"\n')
        self._shim("osascript", f'cat >/dev/null; [ -f "{self.tab_flag}" ] && echo yes || echo no\n')
        self._shim("pgrep", "exit 0\n")          # "Chrome is running"
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Health)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.stamp = self.home / ".board-steward" / f".opened-{self.port}"

    def _shim(self, name, body):
        p = self.bin / name
        p.write_text("#!/usr/bin/env bash\n" + body); p.chmod(0o755)

    def run(self, **env):
        e = {**os.environ, "HOME": str(self.home),
             "PATH": f"{self.bin}:{os.environ['PATH']}"}
        for k in ("BOARD_NO_AUTO_OPEN", "BOARD_OPEN_EXPLICIT", "CLAUDE_CODE_SESSION_ID"):
            e.pop(k, None)
        e.update(env)
        subprocess.run(["bash", str(SCRIPT), str(self.port), "", "sid-test"],
                       env=e, check=True, timeout=20)
        time.sleep(0.3)                          # the `open` is backgrounded

    def opens(self) -> int:
        return len(self.open_log.read_text().splitlines()) if self.open_log.exists() else 0

    def age_stamp(self, seconds):
        """Push the stamp's mtime past the #122 cooldown without changing its day."""
        t = time.time() - seconds
        os.utime(self.stamp, (t, t))

    def close(self):
        self.srv.shutdown()


def scenario():
    global _fails
    with tempfile.TemporaryDirectory() as td:
        r = Rig(Path(td))
        try:
            print("passive (SessionStart hook)")
            r.run()
            check(r.opens() == 1, "first session of the day, no tab → opens once")
            check("sid=sid-test" in r.open_log.read_text(), "opened URL carries ?sid")

            r.age_stamp(COOLDOWN + 60)           # tab closed, a later session starts
            r.run()
            check(r.opens() == 1, "later session same day, tab closed → does NOT re-open")

            r.run(); r.run()
            check(r.opens() == 1, "three more sessions → still one tab today")

            print("explicit (bootstrap / board-new)")
            r.run(BOARD_OPEN_EXPLICIT="1")
            check(r.opens() == 2, "explicit request same day, no tab → opens")
            r.run(BOARD_OPEN_EXPLICIT="1")
            check(r.opens() == 2, "explicit burst inside the cooldown → suppressed (#122)")

            print("tab already visible")
            r.tab_flag.touch()
            r.age_stamp(COOLDOWN + 60)
            r.run(BOARD_OPEN_EXPLICIT="1"); r.run()
            check(r.opens() == 2, "a visible tab never gets a duplicate (either mode)")
            r.tab_flag.unlink()

            print("next day")
            yesterday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
            r.stamp.write_text(yesterday + "\n"); r.age_stamp(86400)
            r.run()
            check(r.opens() == 3, "yesterday's stamp → first session today opens again")
            r.age_stamp(COOLDOWN + 60); r.run()
            check(r.opens() == 3, "…and only once")

            print("legacy + opt-out")
            r.stamp.write_text(""); r.age_stamp(COOLDOWN + 60)   # pre-fix empty stamp
            r.run()
            check(r.opens() == 4, "legacy empty stamp is not 'today' → opens, then self-heals")
            check(r.stamp.read_text().strip() == date.today().strftime("%Y%m%d"),
                  "stamp now records today's date")
            r.stamp.unlink()
            r.run(BOARD_NO_AUTO_OPEN="1")
            check(r.opens() == 4, "BOARD_NO_AUTO_OPEN=1 suppresses the open")
        finally:
            r.close()


def main():
    global _fails
    d0 = date.today()
    scenario()
    if _fails and date.today() != d0:            # straddled midnight → one clean rerun
        print("\n(date rolled over mid-run — rerunning)\n"); _fails = 0; scenario()
    print(f"\n{'PASS' if not _fails else f'FAIL ({_fails})'}")
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
