#!/usr/bin/env python3
"""#959 - "one WorkBoard per Chrome": the auto-open gate and its tracker.

Each scenario below is a cause that was PROVED on a real machine (see the #959
card) or a gate the fix relies on:

  1. another board's tab is open (different port)   -> passive never opens;
     an explicit request for THIS board still does (per-port match).
  2. the once-per-day gate is global across boards  -> board A opened today,
     a session landing on board B opens nothing.
  3. SessionStart source=compact                    -> never opens (the 01:07
     night tab).
  4. Chrome not running / no window yet             -> deferred pass: launch
     Chrome bare, wait for its restored windows, open only if no board tab
     came back.
  5. AppleScript unavailable                        -> /health viewer fallback
     across every registered board, not just this port.
  6. host alias + path/hash URLs                    -> localhost:PORT/#x counts.
  7. every decision is one JSON line in the tracker with the fields the doctor
     and `card.py autoopen-log` need.
  8. headless `claude -p` workers carry BOARD_NO_AUTO_OPEN=1; the hook forwards
     the SessionStart source and a caller tag.

Harness: real board_autoopen.sh -> _autoopen.py, real /health over HTTP,
throwaway HOME; `open`, `osascript` and `pgrep` are PATH shims so no browser is
touched. Run: python3 dev/test_autoopen_959.py  -> exit 0 = green.
"""
from __future__ import annotations
import http.server, json, os, subprocess, sys, tempfile, threading, time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "board_autoopen.sh"
sys.path.insert(0, str(ROOT / "scripts"))

_fails = 0
def check(cond, msg):
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond: _fails += 1


class _Health(http.server.BaseHTTPRequestHandler):
    sse = {}   # port -> sseClients
    def do_GET(self):
        n = self.sse.get(self.server.server_address[1], 0)
        body = json.dumps({"ok": True, "sseClients": n, "lastSseConnectMs": 0, "nowMs": 1}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass


def _serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Rig:
    def __init__(self, tmp: Path):
        self.home = tmp / "home"; (self.home / ".board-steward").mkdir(parents=True)
        self.bin = tmp / "bin"; self.bin.mkdir()
        self.state = tmp / "state"; self.state.mkdir()
        self.open_log = tmp / "open.log"
        self.a = _serve(); self.b = _serve()
        self.pa, self.pb = self.a.server_address[1], self.b.server_address[1]
        # board B is a registered board (a pinned port outside the window still counts)
        (self.home / ".board-steward" / "port-registry.json").write_text(json.dumps(
            {"/proj/b/board": {"pid": 1, "port": self.pb, "started_at": "2026-01-01T00:00:00Z"}}))
        self.log = self.home / ".board-steward" / "autoopen.jsonl"
        self._shim("open", f'echo "$@" >> "{self.open_log}"\n')
        self._shim("osascript", f'cat >/dev/null; cat "{self.state}/as.txt"; exit "$(cat "{self.state}/as.rc")"\n')
        self._shim("pgrep", f'cat "{self.state}/pgrep.txt"; [ -s "{self.state}/pgrep.txt" ]\n')
        self.chrome(running=True, windows=1, tabs=[])

    def _shim(self, name, body):
        p = self.bin / name
        p.write_text("#!/usr/bin/env bash\n" + body); p.chmod(0o755)

    def chrome(self, running=True, windows=1, tabs=(), rc=0):
        (self.state / "pgrep.txt").write_text("4242\n" if running else "")
        (self.state / "as.rc").write_text(str(rc))
        (self.state / "as.txt").write_text("pid:4242\n" + f"windows:{windows}\n" + "".join(t + "\n" for t in tabs))

    def run(self, port=None, wait_deferred=False, **env):
        port = port or self.pa
        e = {**os.environ, "HOME": str(self.home), "PATH": f"{self.bin}:{os.environ['PATH']}"}
        for k in ("BOARD_NO_AUTO_OPEN", "BOARD_OPEN_EXPLICIT", "BOARD_OPEN_SOURCE",
                  "BOARD_OPEN_CALLER", "CLAUDE_CODE_SESSION_ID", "BOARD_STEWARD_HOME"):
            e.pop(k, None)
        e.update(env)
        before = len(self.rows())
        p = subprocess.Popen(["bash", str(SCRIPT), str(port), "", "sid-959"], env=e, start_new_session=True)
        assert p.wait(timeout=20) == 0
        deadline = time.time() + (40 if wait_deferred else 10)
        while time.time() < deadline:
            rows = self.rows()[before:]
            if wait_deferred:
                if any(r.get("phase") == "deferred" for r in rows): break
            else:
                try: os.killpg(p.pid, 0)
                except ProcessLookupError: break
            time.sleep(0.05)
        return self.rows()[before:]

    def rows(self):
        if not self.log.exists(): return []
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]

    def opens(self):
        return [l for l in self.open_log.read_text().splitlines()] if self.open_log.exists() else []

    def url_opens(self):
        return [l for l in self.opens() if "http" in l]

    def reset_day(self):
        for p in (self.home / ".board-steward").glob(".opened-*"):
            p.unlink()

    def age_stamps(self, seconds=60):
        for p in (self.home / ".board-steward").glob(".opened-*"):
            t = time.time() - seconds; os.utime(p, (t, t))

    def close(self):
        self.a.shutdown(); self.b.shutdown()


def scenario():
    with tempfile.TemporaryDirectory() as td:
        r = Rig(Path(td))
        try:
            print("1. another board's tab is open")
            r.chrome(tabs=["http://127.0.0.1:7893/?sid=abc"])
            rows = r.run(BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="startup")
            check(rows and rows[-1]["action"] == "skip" and rows[-1]["reason"] == "tab_present",
                  "passive: a tab on ANOTHER port -> skip (tab_present)")
            check(rows[-1]["chrome"]["board_tabs"] == [{"port": 7893, "url": "127.0.0.1:7893/"}],
                  "tracker lists the matching board tab as a board URL without its query")
            check(rows[-1]["chrome"]["match_mode"] == "any_board", "passive match mode is any_board")
            check(r.url_opens() == [], "nothing opened")
            rows = r.run(BOARD_OPEN_EXPLICIT="1", BOARD_OPEN_CALLER="board-new")
            check(rows[-1]["action"] == "open" and rows[-1]["chrome"]["match_mode"] == "this_port",
                  "explicit (board-new): another port's tab does not count -> opens THIS board")
            check(len(r.url_opens()) == 1 and f"127.0.0.1:{r.pa}/?sid=sid-959" in r.url_opens()[0],
                  "opened URL is this board with ?sid")

            print("2. once-per-day is global across boards")
            r.age_stamps()
            r.chrome(tabs=[])
            rows = r.run(port=r.pb, BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="startup")
            check(rows[-1]["action"] == "skip" and rows[-1]["reason"] == "once_per_day",
                  "board A opened today -> a session landing on board B opens nothing")
            stamp = r.home / ".board-steward" / ".opened-any"
            check(stamp.exists() and stamp.read_text().strip() == date.today().strftime("%Y%m%d"),
                  "global .opened-any stamp carries today's date")
            r.reset_day()
            rows = r.run(port=r.pb, BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="resume")
            check(rows[-1]["action"] == "open", "no stamp today, no tab -> resume opens once")
            check(rows[-1]["source"] == "resume" and rows[-1]["caller"] == "session-start",
                  "tracker records caller and SessionStart source")

            print("3. compaction is not a session")
            r.reset_day(); r.chrome(tabs=[])
            n = len(r.url_opens())
            rows = r.run(BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="compact")
            check(rows[-1]["action"] == "skip" and rows[-1]["reason"] == "compact_is_not_a_session",
                  "source=compact -> skip, logged")
            check(len(r.url_opens()) == n, "nothing opened on compaction")

            print("4. Chrome not running: deferred pass waits for session restore")
            r.reset_day(); r.chrome(running=False)
            n = len(r.url_opens())
            # flip Chrome to "up, with a RESTORED board tab" ~1s after the launch
            def restore(tabs): time.sleep(1.0); r.chrome(running=True, windows=1, tabs=tabs)
            threading.Thread(target=restore, args=(["http://localhost:7891/"],), daemon=True).start()
            rows = r.run(wait_deferred=True, BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="startup")
            main = [x for x in rows if x["phase"] == "main"][-1]
            deferred = [x for x in rows if x["phase"] == "deferred"][-1]
            check(main["action"] == "defer" and main["reason"] == "chrome_not_running",
                  "main pass: Chrome not running -> defer (never opens with the URL)")
            check(any(l.strip() == "-a Google Chrome" for l in r.opens()),
                  "Chrome launched BARE (no URL), so its own session restore runs first")
            check(deferred["action"] == "skip" and deferred["reason"] == "tab_restored",
                  "deferred pass: a restored board tab -> skip (tab_restored)")
            check(len(r.url_opens()) == n, "no URL opened: the restored tab is the one board")
            check(not (r.home / ".board-steward" / ".opening.lock").exists(), "lock released after the deferred pass")

            r.reset_day(); r.age_stamps(); r.chrome(running=True, windows=0, tabs=[])
            threading.Thread(target=restore, args=([],), daemon=True).start()
            rows = r.run(wait_deferred=True, BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="startup")
            deferred = [x for x in rows if x["phase"] == "deferred"][-1]
            check([x for x in rows if x["phase"] == "main"][-1]["reason"] == "chrome_no_window",
                  "Chrome running with no window yet -> defer")
            check(deferred["action"] == "open" and deferred["reason"] == "no_board_tab_after_launch",
                  "deferred pass: nothing restored -> opens exactly once")
            check(len(r.url_opens()) == n + 1, "one URL open")

            print("5. AppleScript unavailable -> /health viewers across every board")
            r.reset_day(); r.age_stamps(); r.chrome(rc=1)
            _Health.sse[r.pb] = 1                 # someone is viewing board B
            n = len(r.url_opens())
            rows = r.run(BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="startup")
            check(rows[-1]["chrome"]["state"] == "unknown" and rows[-1]["chrome"]["how"].startswith("applescript-rc1"),
                  "tracker says how the Chrome state was (not) determined")
            check(rows[-1]["action"] == "skip" and rows[-1]["reason"] == "sse_viewer_present"
                  and rows[-1]["gates"]["sse_fallback"]["ports_with_viewer"] == [r.pb],
                  "a viewer on ANOTHER registered board -> skip")
            _Health.sse[r.pb] = 0
            rows = r.run(BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="startup")
            check(rows[-1]["action"] == "open" and rows[-1]["reason"] == "chrome_unknown_no_viewer",
                  "no viewer anywhere -> opens")
            check(len(r.url_opens()) == n + 1, "one URL open")

            print("6. host alias + path/hash URLs")
            r.reset_day(); r.age_stamps()
            r.chrome(tabs=["http://localhost:7899/#card-12"])
            rows = r.run(BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="startup")
            check(rows[-1]["reason"] == "tab_present" and rows[-1]["matched"] == ["localhost:7899/"],
                  "localhost:PORT/#hash counts as a board tab")
            r.chrome(tabs=["http://127.0.0.1:5173/", "http://localhost:8000/docs"])
            rows = r.run(BOARD_OPEN_CALLER="session-start", BOARD_OPEN_SOURCE="startup")
            check(rows[-1]["action"] == "open" and rows[-1]["chrome"]["loopback_tabs"] == 2
                  and rows[-1]["chrome"]["board_tabs"] == [],
                  "other local dev servers are counted but never logged as board tabs")

            print("7. tracker fields")
            need = {"ts", "caller", "source", "sid", "cwd", "port", "pid", "ppid", "parent", "tty",
                    "gates", "chrome", "action", "reason", "phase", "explicit"}
            rows = r.rows()
            check(all(need <= set(x) for x in rows), f"every line carries {sorted(need)}")
            check(all(x["sid"] == "sid-959" and x["port"] in (r.pa, r.pb) for x in rows), "sid + port recorded")
            check(all(x["parent"] for x in rows), "parent process name recorded")
            r.run(BOARD_NO_AUTO_OPEN="1")
            check(r.rows()[-1]["reason"] == "no_auto_open", "BOARD_NO_AUTO_OPEN=1 is logged, not silent")
            r.a.shutdown()
            check(r.run()[-1]["reason"] == "server_down", "server down is logged")
            import _autoopen
            os.environ["HOME"] = str(r.home)
            txt = _autoopen.format_log(r.rows())
            check("tab_present" in txt and "once_per_day" in txt and "deferred" not in txt.split("\n")[0],
                  "format_log renders the trail")

            print("8. headless workers + hook wiring")
            import importlib
            os.environ.pop("BOARD_NO_AUTO_OPEN", None)
            hc = importlib.import_module("hourly_common")
            check(hc._LLM_ENV.get("BOARD_NO_AUTO_OPEN") == "1", "hourly `claude -p` env carries BOARD_NO_AUTO_OPEN=1")
            hook = (ROOT / "scripts" / "hook_session_start.sh").read_text()
            check('BOARD_OPEN_SOURCE="${hook_source}"' in hook and 'BOARD_OPEN_CALLER="session-start"' in hook,
                  "SessionStart hook forwards source + caller to the gate")
            check('d.get("source"' in hook, "SessionStart hook parses `source` from its stdin payload")
        finally:
            try: r.close()
            except Exception: pass


def main():
    global _fails
    d0 = date.today()
    scenario()
    if _fails and date.today() != d0:
        print("\n(date rolled over mid-run - rerunning)\n"); _fails = 0; scenario()
    print(f"\n{'PASS' if not _fails else f'FAIL ({_fails})'}")
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
