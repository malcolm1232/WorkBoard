#!/usr/bin/env python3
"""#960 UI - a board created while a WorkBoard tab is open shows up in that
tab's project switcher with a "New board: <name>" notice, with no reload, and
the notice's Open button swaps to it in place.

Real serve.py servers in a throwaway HOME, a real headless Chromium at laptop
width (1440x900). Board A is open in the page; board B is then created with
`serve.py --bootstrap` exactly as automatic creation does, and B's server
announces itself to A (POST /boards/notify → SSE boards-changed).

Run: python3 dev/test_960_ui_notice.py  → exit 0 = green.
Screenshots: $TMPDIR/t960ui-*/notice.png
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVE = str(REPO / "scripts" / "serve.py")

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'ok ' if cond else 'FAIL'} {msg}")
    if not cond:
        _fails += 1


def free_port(lo=7960, hi=7995, skip=()):
    for p in range(lo, hi):
        if p in skip:
            continue
        with socket.socket() as s:
            s.settimeout(0.1)
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise SystemExit("no free port")


def wait_health(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def main():
    state = Path(os.path.realpath(tempfile.mkdtemp(prefix="t960ui-")))
    home = state / "home"
    (home / ".board-steward").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_")}
    env.pop("CLAUDECODE", None)
    env.update(HOME=str(home), BOARD_REGISTRY=str(state / "registry.json"),
               BOARD_ASSIGNMENTS=str(state / "assignments.json"),
               BOARD_ACTIVE=str(state / "last-active"))
    a_root, b_root = home / "Desktop" / "alpha-app", home / "Desktop" / "beta-site"
    a_root.mkdir(parents=True)
    b_root.mkdir(parents=True)
    pa = free_port()
    pb = free_port(skip=(pa,))
    procs = []
    try:
        procs.append(subprocess.Popen(
            [sys.executable, SERVE, "--project", str(a_root), "--bootstrap", "--no-discover",
             "--title", "alpha-app", "--port", str(pa)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if not wait_health(pa):
            raise SystemExit("board A never came up")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            page.goto(f"http://127.0.0.1:{pa}/", wait_until="domcontentloaded")
            page.wait_for_selector("#board-title", timeout=15000)
            page.wait_for_function("() => window._sseConnected === true || "
                                   "document.querySelector('#status') !== null", timeout=10000)
            page.wait_for_timeout(800)
            tabs0 = page.eval_on_selector_all("#project-tabs .proj-tab", "els => els.length")
            check(tabs0 == 0, "one board only: the switcher is hidden")

            # Board B is created while A's tab is open (what autocreate does).
            procs.append(subprocess.Popen(
                [sys.executable, SERVE, "--project", str(b_root), "--bootstrap", "--no-discover",
                 "--title", "beta-site", "--port", str(pb)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            check(wait_health(pb), "board B is live")

            page.wait_for_selector("#newboard-notice.show", timeout=8000)
            txt = page.inner_text("#newboard-notice .nb-text")
            check(txt.strip() == "New board: beta-site", f"notice reads '{txt.strip()}'")
            names = page.eval_on_selector_all("#project-tabs .proj-tab", "els => els.map(e => e.textContent)")
            check("beta-site" in names and "alpha-app" in names,
                  f"switcher now lists both boards without a reload ({names})")
            ring = page.eval_on_selector_all("#project-tabs .proj-tab.is-new",
                                             "els => els.map(e => e.textContent)")
            check(ring == ["beta-site"], "the new board's tab is ringed")
            reloaded = page.evaluate("() => performance.getEntriesByType('navigation').length")
            check(reloaded == 1, "page was not reloaded")
            box = page.eval_on_selector("#newboard-notice", "e => { const r = e.getBoundingClientRect();"
                                        " return {l: r.left, r: r.right, b: r.bottom, w: innerWidth, h: innerHeight}; }")
            check(box["l"] >= 16 and box["r"] <= box["w"] - 16 and box["b"] <= box["h"] - 8,
                  "notice sits fully on screen, bottom centre")
            page.screenshot(path=str(state / "notice.png"))

            page.click("#newboard-notice .nb-open")
            page.wait_for_function(
                "() => { const a = document.querySelector('#project-tabs .proj-tab.active');"
                " return a && a.textContent === 'beta-site'; }", timeout=8000)
            check(True, "Open swaps the page to the new board in place")
            hidden = page.eval_on_selector("#newboard-notice", "e => !e.classList.contains('show')")
            check(hidden, "notice is dismissed after Open")
            page.screenshot(path=str(state / "after-open.png"))
            browser.close()
    finally:
        for pr in procs:
            pr.terminate()
        for pr in procs:
            try:
                pr.wait(timeout=5)
            except Exception:
                pr.kill()
    print(f"\nscreenshots: {state}/notice.png, {state}/after-open.png")
    print("PASS" if _fails == 0 else f"{_fails} FAILED")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
