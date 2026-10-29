#!/usr/bin/env python3
"""#961 - "+ Add column" must not force sideways scroll on a board that fits.

The button used to be the last flex item in the #board row, so its width plus
a gap was added to #board's scroll extent: the default 5-wide board at 1440px
fit the window, yet #board scrolled sideways only to reveal a sliver of the
button past the right edge. It is now a "+" segment in the header's
column-tools pill (next to Reset Columns / Sort Time).

Asserts, on a freshly bootstrapped board in a real headless Chromium:
  - 1280, 1440, 1920 wide: no page-level horizontal scroll, and the header's
    Add column control sits fully on screen.
  - 1440 and 1920 wide: #board has no horizontal scroll (the columns fit).
  - nothing but columns sits in the #board row (no trailing button).
  - clicking it adds a column through the name prompt.
  - the header title keeps its centered slot at 1440px (the control is compact).
  - with many columns, #board itself still scrolls sideways (overflow-x auto)
    while the page does not.

Isolated: throwaway HOME + BOARD_* registries, one real serve.py on a high port.

Run: python3 dev/test_961_add_column_overflow.py  -> exit 0 = green.
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


def free_port(lo=18960, hi=18999):
    for p in range(lo, hi):
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


MEASURE = """() => {
    const d = document.documentElement, b = document.querySelector('#board');
    const btn = document.querySelector('#add-col-btn');
    const r = btn ? btn.getBoundingClientRect() : null;
    const strays = [...b.children].filter(e =>
        !e.classList.contains('column-stack') && !e.classList.contains('column'))
        .map(e => e.tagName + '.' + e.className);
    return {pageOver: d.scrollWidth - d.clientWidth,
            bodyOver: document.body.scrollWidth - document.body.clientWidth,
            boardOver: b.scrollWidth - b.clientWidth,
            boardOvf: getComputedStyle(b).overflowX,
            btn: r && getComputedStyle(btn).display !== 'none'
                 ? {l: r.left, r: r.right, t: r.top, b: r.bottom} : null,
            vw: innerWidth, strays,
            inflow: document.querySelector('header').classList.contains('title-inflow'),
            cols: document.querySelectorAll('#board .column').length};
}"""


def main():
    state = Path(os.path.realpath(tempfile.mkdtemp(prefix="t961-")))
    home = state / "home"
    (home / ".board-steward").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_")}
    env.pop("CLAUDECODE", None)
    env.update(HOME=str(home), BOARD_REGISTRY=str(state / "registry.json"),
               BOARD_ASSIGNMENTS=str(state / "assignments.json"),
               BOARD_ACTIVE=str(state / "last-active"), BOARD_NO_AUTO_OPEN="1")
    root = home / "Desktop" / "alpha-app"
    root.mkdir(parents=True)
    port = free_port()
    (state / "assignments.json").write_text(json.dumps({str(root / "board"): port}))
    srv = subprocess.Popen(
        [sys.executable, SERVE, "--project", str(root), "--bootstrap", "--no-discover",
         "--title", "alpha-app", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_health(port):
            raise SystemExit("test server never came up")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for w in (1280, 1440, 1920):
                page = browser.new_context(viewport={"width": w, "height": 900}).new_page()
                page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                page.wait_for_selector("#board .column", timeout=15000)
                page.wait_for_timeout(500)
                m = page.evaluate(MEASURE)
                print(f"{w}px: {m}")
                check(m["pageOver"] <= 0 and m["bodyOver"] <= 0,
                      f"{w}: no page-level horizontal scroll")
                check(m["btn"] is not None and m["btn"]["l"] >= 0
                      and m["btn"]["r"] <= m["vw"] and m["btn"]["b"] <= 48,
                      f"{w}: Add column is in the header, fully on screen")
                check(not m["strays"], f"{w}: only columns in the #board row ({m['strays']})")
                if w >= 1440:
                    check(not m["inflow"],
                          f"{w}: header title keeps its centered slot")
                    check(m["boardOver"] <= 1,
                          f"{w}: default board fits, #board does not scroll sideways "
                          f"(over by {m['boardOver']})")
                page.screenshot(path=str(state / f"fit-{w}.png"))
                page.context.close()

            # Interaction checks run last, in their own context, so the columns
            # they add can never leak into a width measurement above.
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#board .column", timeout=15000)
            n0 = page.evaluate(MEASURE)["cols"]
            page.click("#add-col-btn")
            page.wait_for_selector("#mp-input", state="visible", timeout=5000)
            page.fill("#mp-input", "In Review")
            page.click("#mp-ok")
            page.wait_for_function(
                f"() => document.querySelectorAll('#board .column').length === {n0 + 1}",
                timeout=5000)
            names = page.eval_on_selector_all(
                "#board .column .name", "els => els.map(e => e.textContent.trim())")
            check("In Review" in names, "clicking Add column adds the column")

            # Many columns: the columns area scrolls, the page does not.
            page.evaluate("""() => { for (let i = 0; i < 8; i++)
                state.columns.push({id: 'x' + i, name: 'Extra ' + i, kind: 'custom'});
                render(); }""")
            page.wait_for_timeout(200)
            m2 = page.evaluate(MEASURE)
            check(m2["boardOver"] > 0 and m2["boardOvf"] == "auto",
                  f"many columns: #board scrolls sideways (over by {m2['boardOver']})")
            check(m2["pageOver"] <= 0, "many columns: the page itself still does not")
            page.context.close()
            browser.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()
    print(f"\nscreenshots: {state}/fit-*.png")
    print("PASS" if _fails == 0 else f"{_fails} FAILED")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
