#!/usr/bin/env python3
"""Auto-open gate for the board (#73, #122, #907, #959) with a built-in tracker.

board_autoopen.sh is the stable entry point (same argv and env contract as
before); it execs this module. Every caller funnels through here, so every open
DECISION, taken or skipped, is one JSON line in ~/.board-steward/autoopen.jsonl
(read it with `card.py autoopen-log`).

Policy (#959 - "one WorkBoard per Chrome"):
  * PASSIVE callers (the SessionStart hook) open nothing while ANY board tab is
    visible in Chrome - any registered board, any port in the board window, as
    127.0.0.1 or localhost. The board has a project switcher (#841/#846), so one
    tab reaches every board; a second tab for another project is the duplicate
    the user kept seeing.
  * The passive once-per-day gate (#907) is GLOBAL across boards, not per port:
    closing the only board tab is a decision, respected until tomorrow whatever
    project the next session starts in.
  * A compaction (SessionStart source=compact) never opens: it is not a new
    session, and it fired at 01:07 at night in a proven case.
  * EXPLICIT callers (BOARD_OPEN_EXPLICIT=1 - bootstrap, card.py board-new) asked
    for THIS board, so only a tab on this port counts and the day gate is skipped;
    the cooldown and the lock still apply.
  * Chrome not running, or running with no window yet (right after login, when
    macOS is still restoring it): a DEFERRED pass launches Chrome bare, waits for
    its windows (session restore included), re-checks the tabs, and opens only if
    no board tab came back. Launching Chrome WITH the URL used to race the
    restore and land a second board tab beside the restored one.
  * The tab query has a hard timeout; a hung Chrome or a slow AppleScript never
    blocks session start, it degrades to the /health viewer fallback.

Usage: _autoopen.py <port> [project_dir] [session_id]
Env:   BOARD_NO_AUTO_OPEN=1   suppress everything (headless/CI/cron)
       BOARD_OPEN_EXPLICIT=1  explicit request (skips the day gate, per-port match)
       BOARD_OPEN_CALLER      free-text caller tag for the log (hook/bootstrap/...)
       BOARD_OPEN_SOURCE      SessionStart source (startup|resume|clear|compact)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PORT_LO, PORT_HI = 7891, 7999          # mirrors port_registry.PORT_LO/HI
COOLDOWN_S = 12                        # #122 burst/periodic dedupe window
LOCK_STALE_S = 60                      # a deferred pass may hold it ~30s
AS_TIMEOUT_S = 6.0                     # AppleScript watchdog
DEFER_WAIT_S = 25.0                    # wait for Chrome windows after launch
DEFER_SETTLE_S = 2.0                   # extra settle once a window exists
HEALTH_TIMEOUT_S = 0.4

STATE_DIR = Path(os.environ.get("BOARD_STEWARD_HOME") or (Path.home() / ".board-steward"))
LOG_PATH = STATE_DIR / "autoopen.jsonl"
LOG_MAX_BYTES = 2_000_000              # rotate once, never grow unbounded

_LOOPBACK_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost):(\d{4,5})(/[^?#]*)?", re.I)


# ---------------------------------------------------------------- utilities

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _parent_info(pid: int) -> tuple[str, str]:
    """(command name, controlling tty) of a pid via ps; ('', '') if unknown."""
    try:
        out = subprocess.run(["ps", "-o", "comm=,tty=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=1).stdout.strip()
        parts = out.split()
        if not parts:
            return "", ""
        return os.path.basename(parts[0]), (parts[1] if len(parts) > 1 else "")
    except Exception:
        return "", ""


def _board_ports() -> set[int]:
    """Every port that is, or was, a board: the registry + assignments. Falls
    back to just the window bounds (the URL filter still applies)."""
    ports: set[int] = set()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import port_registry as pr
        for v in pr.read().values():
            if isinstance(v, dict) and v.get("port"):
                ports.add(int(v["port"]))
        for p in pr.assignments().values():
            ports.add(int(p))
    except Exception:
        pass
    return ports


def _health(port: int, timeout: float = HEALTH_TIMEOUT_S) -> dict | None:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None


def _viewer_recent(h: dict | None) -> bool:
    """#150 - a live SSE client, or one that connected in the last 20s."""
    if not h:
        return False
    try:
        n = int(h.get("sseClients", 0) or 0)
        now = int(h.get("nowMs", 0) or 0)
        last = int(h.get("lastSseConnectMs", 0) or 0)
        return n > 0 or bool(now and last and (now - last) < 20000)
    except Exception:
        return False


def _board_url(url: str, known: set[int] = frozenset()) -> tuple[int, str] | None:
    """(port, 'host:port/path') if url is a loopback URL on a board port: the
    designation window, or a port the registry knows (a pinned custom port)."""
    m = _LOOPBACK_RE.match(url or "")
    if not m:
        return None
    port = int(m.group(2))
    if not (PORT_LO <= port <= PORT_HI) and port not in known:
        return None
    return port, f"{m.group(1).lower()}:{port}{m.group(3) or '/'}"


# ---------------------------------------------------------------- chrome probe

_AS_QUERY = '''
tell application "System Events"
  if not (exists process "Google Chrome") then return "absent"
  set pidline to "pid:" & (unix id of process "Google Chrome")
end tell
tell application "Google Chrome"
  set out to pidline & linefeed & "windows:" & (count of windows) & linefeed
  repeat with w in windows
    repeat with t in tabs of w
      set u to URL of t
      if (u starts with "http://127.0.0.1:") or (u starts with "http://localhost:") then
        set out to out & u & linefeed
      end if
    end repeat
  end repeat
  return out
end tell
'''


def chrome_running_pids() -> list[int]:
    try:
        out = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True,
                             text=True, timeout=2).stdout.split()
        return [int(x) for x in out if x.isdigit()]
    except Exception:
        return []


def probe_chrome(known: set[int] = frozenset()) -> dict:
    """Read-only. Returns {state, how, pids, as_pid, windows, loopback_tabs, board_tabs}.
    state: tab | no_tab | no_window | not_running | unknown. `known` = registry
    ports that count as boards even outside the designation window."""
    res = {"state": "unknown", "how": "", "pids": [], "as_pid": None,
           "windows": None, "loopback_tabs": 0, "board_tabs": []}
    if not shutil.which("osascript"):
        res["how"] = "no-osascript"
        return res
    res["pids"] = chrome_running_pids()
    if not res["pids"]:
        res.update(state="not_running", how="pgrep")
        return res
    try:
        p = subprocess.run(["osascript", "-"], input=_AS_QUERY, capture_output=True,
                           text=True, timeout=AS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        res["how"] = f"applescript-timeout>{AS_TIMEOUT_S:g}s"
        return res
    except Exception as e:  # pragma: no cover - exotic
        res["how"] = f"applescript-error:{type(e).__name__}"
        return res
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        res["how"] = "applescript-rc%d:%s" % (p.returncode, (err[-1] if err else "")[:80])
        return res
    text = p.stdout.strip()
    if text == "absent":
        res.update(state="not_running", how="applescript-absent")
        return res
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    tabs: list[tuple[int, str]] = []
    for ln in lines:
        if ln.startswith("pid:"):
            try:
                res["as_pid"] = int(ln[4:])
            except ValueError:
                pass
        elif ln.startswith("windows:"):
            try:
                res["windows"] = int(ln[8:])
            except ValueError:
                pass
        elif ln.startswith("http"):
            res["loopback_tabs"] += 1
            bt = _board_url(ln, known)
            if bt:
                tabs.append(bt)
        # legacy yes/no protocol (older shims) - "yes" means a board tab exists
        elif ln == "yes":
            tabs.append((0, "legacy:yes"))
    res["board_tabs"] = tabs
    res["how"] = "applescript"
    if res["windows"] == 0 and not tabs:
        res["state"] = "no_window"
    else:
        res["state"] = "tab" if tabs else "no_tab"
    return res


# ---------------------------------------------------------------- tracker

def log_decision(rec: dict) -> None:
    """One JSON line per decision. Never raises, never blocks."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                LOG_PATH.replace(LOG_PATH.with_suffix(".jsonl.1"))
        except Exception:
            pass
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
    except Exception:
        pass


def read_log(limit: int = 30) -> list[dict]:
    rows: list[dict] = []
    for p in (LOG_PATH.with_suffix(".jsonl.1"), LOG_PATH):
        try:
            with open(p) as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return rows[-limit:] if limit else rows


def format_log(rows: list[dict]) -> str:
    if not rows:
        return f"(no auto-open decisions logged yet - {LOG_PATH})"
    out = []
    for r in rows:
        ch = r.get("chrome") or {}
        tabs = ",".join(t.get("url", "") for t in ch.get("board_tabs", [])) or "-"
        out.append("  ".join([
            r.get("ts", "?"),
            f"{r.get('action', '?'):<8}",
            f"{r.get('reason', ''):<22}",
            f":{r.get('port', '?')}",
            f"{(r.get('caller') or '?')}{('/' + r['source']) if r.get('source') else ''}",
            f"chrome={ch.get('state') or '-'}",
            f"tabs={tabs}",
            f"sid={(r.get('sid') or '-')[:8]}",
        ]))
    return "\n".join(out)


# ---------------------------------------------------------------- the gate

class Gate:
    def __init__(self, port: int, project_dir: str, sid: str, phase: str = "main"):
        self.port = port
        self.project_dir = project_dir
        self.sid = sid
        self.phase = phase
        self.explicit = os.environ.get("BOARD_OPEN_EXPLICIT", "0") == "1"
        self.source = os.environ.get("BOARD_OPEN_SOURCE", "") or ""
        self.caller = os.environ.get("BOARD_OPEN_CALLER", "") or ""
        self.url = f"http://127.0.0.1:{port}"
        self.open_url = f"{self.url}/?sid={sid}" if sid else self.url
        self.stamp_any = STATE_DIR / ".opened-any"
        self.stamp_port = STATE_DIR / f".opened-{port}"
        self.lock = STATE_DIR / ".opening.lock"
        self.hold_lock = False
        self.known = _board_ports() | {port}
        pid, ppid = os.getpid(), os.getppid()
        pname, ptty = _parent_info(ppid)
        if not self.caller:
            self.caller = pname or "unknown"
        self.rec: dict = {
            "ts": _now_iso(), "phase": phase, "caller": self.caller,
            "source": self.source or None, "explicit": self.explicit,
            "sid": sid or None, "cwd": os.getcwd(), "port": port,
            "pid": pid, "ppid": ppid, "parent": pname, "tty": ptty or None,
            "gates": {}, "chrome": None, "action": None, "reason": None,
        }

    # -- helpers
    def finish(self, action: str, reason: str, **extra) -> int:
        self.rec.update(action=action, reason=reason, **extra)
        log_decision(self.rec)
        if self.hold_lock:
            try:
                self.lock.rmdir()
            except Exception:
                pass
        return 0

    def _stamp_age(self) -> float | None:
        ages = []
        for p in (self.stamp_any, self.stamp_port):
            try:
                ages.append(time.time() - p.stat().st_mtime)
            except Exception:
                continue
        return min(ages) if ages else None

    def _day_opened(self) -> str:
        """Day recorded in the GLOBAL stamp (passive gate). Legacy: a per-port
        stamp with today's date also counts, so an upgrade mid-day does not
        re-open a board the old code already opened today."""
        for p in (self.stamp_any, self.stamp_port):
            try:
                day = p.read_text().split("\n", 1)[0].strip()
            except Exception:
                continue
            if day == _today():
                return day
        return ""

    def _write_stamps(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        for p in (self.stamp_any, self.stamp_port):
            try:
                p.write_text(_today() + "\n")
            except Exception:
                pass
        if self.project_dir:
            for old in Path(self.project_dir).joinpath("board").glob(".opened-*"):
                try:
                    old.unlink()
                except Exception:
                    pass

    def _take_lock(self) -> bool:
        try:
            if self.lock.is_dir() and time.time() - self.lock.stat().st_mtime >= LOCK_STALE_S:
                self.lock.rmdir()
        except Exception:
            pass
        try:
            self.lock.mkdir()
        except FileExistsError:
            return False
        except Exception:
            return True   # unwritable state dir: do not block the open
        self.hold_lock = True
        return True

    def _matches(self, tabs: list[tuple[int, str]]) -> list[tuple[int, str]]:
        if self.explicit:
            return [t for t in tabs if t[0] in (self.port, 0)]
        return list(tabs)

    def _sse_fallback(self) -> list[int]:
        """Ports with a recent viewer: this port (explicit) or every live board."""
        ports = {self.port} if self.explicit else (_board_ports() | {self.port})
        return sorted(p for p in ports if _viewer_recent(_health(p, 0.6)))

    def _launch(self, with_url: bool) -> str:
        """Fire the browser, detached. Returns the command used."""
        target = self.open_url
        if shutil.which("open"):
            cmd = ["open", "-a", "Google Chrome"] + ([target] if with_url else [])
            try:
                r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=10)
                if r.returncode == 0:
                    return " ".join(cmd)
            except Exception:
                pass
            if with_url:
                subprocess.Popen(["open", target], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return "open " + target
            return "open -a Google Chrome (failed)"
        if with_url and shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return "xdg-open " + target
        return "no-opener"

    # -- main pass
    def run(self) -> int:
        g = self.rec["gates"]
        if os.environ.get("BOARD_NO_AUTO_OPEN", "0") == "1":
            return self.finish("skip", "no_auto_open")
        if not self.explicit and self.source == "compact":
            return self.finish("skip", "compact_is_not_a_session")
        h = _health(self.port)
        if h is None:
            return self.finish("skip", "server_down")
        g["health"] = "ok"

        day = self._day_opened()
        g["once_per_day"] = {"opened_today": bool(day), "applies": not self.explicit}
        if not self.explicit and day:
            return self.finish("skip", "once_per_day")

        age = self._stamp_age()
        g["cooldown"] = {"stamp_age_s": round(age, 1) if age is not None else None, "window_s": COOLDOWN_S}
        if age is not None and age < COOLDOWN_S:
            return self.finish("skip", "cooldown")

        if not self._take_lock():
            g["lock"] = "held"
            return self.finish("skip", "locked")
        g["lock"] = "taken"

        ch = probe_chrome(self.known)
        matches = self._matches(ch["board_tabs"])
        self.rec["chrome"] = {
            "state": ch["state"], "how": ch["how"], "pids": ch["pids"], "as_pid": ch["as_pid"],
            "windows": ch["windows"], "loopback_tabs": ch["loopback_tabs"],
            "board_tabs": [{"port": p, "url": u} for p, u in ch["board_tabs"]],
            "match_mode": "this_port" if self.explicit else "any_board",
        }
        if matches:
            return self.finish("skip", "tab_present", matched=[u for _, u in matches])

        if ch["state"] == "no_tab":
            self._write_stamps()
            return self.finish("open", "no_board_tab", opened=self.open_url, via=self._launch(True))

        if ch["state"] in ("not_running", "no_window"):
            # Arm the stamps NOW so a burst during the deferred wait collapses,
            # then hand the wait to a detached child; the lock travels with it.
            self._write_stamps()
            self.hold_lock = False
            try:
                subprocess.Popen([sys.executable, __file__, "--deferred", str(self.port),
                                  self.project_dir, self.sid],
                                 env={**os.environ, "BOARD_OPEN_CALLER": self.caller},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            except Exception as e:
                self.hold_lock = True
                return self.finish("error", f"deferred_spawn_failed:{type(e).__name__}")
            return self.finish("defer", f"chrome_{ch['state']}")

        # unknown: AppleScript unavailable, denied or timed out -> /health viewers
        viewers = self._sse_fallback()
        g["sse_fallback"] = {"ports_with_viewer": viewers}
        if viewers:
            return self.finish("skip", "sse_viewer_present")
        self._write_stamps()
        return self.finish("open", "chrome_unknown_no_viewer", opened=self.open_url, via=self._launch(True))

    # -- deferred pass (Chrome was not running / had no window yet)
    def run_deferred(self) -> int:
        self.hold_lock = True            # inherited from the main pass
        self.rec["gates"]["deferred"] = True
        pids = chrome_running_pids()
        launched = None
        if not pids:
            launched = self._launch(False)
        deadline = time.time() + DEFER_WAIT_S
        ch = probe_chrome(self.known)
        while time.time() < deadline and ch["state"] in ("not_running", "no_window", "unknown"):
            time.sleep(0.5)
            ch = probe_chrome(self.known)
        if ch["state"] in ("tab", "no_tab"):
            time.sleep(DEFER_SETTLE_S)   # let session restore finish populating
            ch = probe_chrome(self.known)
        matches = self._matches(ch["board_tabs"])
        self.rec["chrome"] = {
            "state": ch["state"], "how": ch["how"], "pids": ch["pids"], "as_pid": ch["as_pid"],
            "windows": ch["windows"], "loopback_tabs": ch["loopback_tabs"],
            "board_tabs": [{"port": p, "url": u} for p, u in ch["board_tabs"]],
            "match_mode": "this_port" if self.explicit else "any_board",
            "launched": launched,
        }
        if matches:
            return self.finish("skip", "tab_restored", matched=[u for _, u in matches])
        if ch["state"] == "unknown":
            viewers = self._sse_fallback()
            self.rec["gates"]["sse_fallback"] = {"ports_with_viewer": viewers}
            if viewers:
                return self.finish("skip", "sse_viewer_present")
        self._write_stamps()
        return self.finish("open", "no_board_tab_after_launch", opened=self.open_url, via=self._launch(True))


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--log":
        n = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 30
        print(format_log(read_log(n)))
        return 0
    deferred = False
    if argv and argv[0] == "--deferred":
        deferred = True
        argv = argv[1:]
    if not argv:
        print("usage: _autoopen.py <port> [project_dir] [session_id]", file=sys.stderr)
        return 2
    try:
        port = int(argv[0])
    except ValueError:
        return 2
    project_dir = argv[1] if len(argv) > 1 else ""
    sid = (argv[2] if len(argv) > 2 else "") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    gate = Gate(port, project_dir, sid, phase="deferred" if deferred else "main")
    try:
        return gate.run_deferred() if deferred else gate.run()
    except Exception as e:   # never fail the caller
        gate.finish("error", f"{type(e).__name__}:{str(e)[:80]}")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
