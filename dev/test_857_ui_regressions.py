#!/usr/bin/env python3
"""#857 code-review findings 7 + 8 — real-browser regressions (Playwright).

  R7. Chip label staleness: the "+N Boards" chip label is seeded only on the
      FIRST reflow; later resize reflows measure the PREVIOUS pass's label
      (e.g. "+2 Boards") while the final render is wider ("+10 Boards"), so
      one tab too many is kept and the chip overhangs the nav — the same
      clipping class #857 fixed, recurring on shrink. Assert: after every
      resize step, the chip's right edge stays inside the nav.
  R8. Title residue: rich/multiline paste into the contenteditable title
      leaves <br>/span residue whose textContent EQUALS the clean title, so
      the conditional `el.textContent = t` never wipes it — the title renders
      two-line until reload. Assert: applyBoardTitle() normalizes the DOM to
      a single text node.

Isolated: temp HOME + BOARD_* registries, 12 throwaway boards, one real
serve.py. Fresh browser context; domcontentloaded (SSE keeps networkidle busy).

Run: python3 dev/test_857_ui_regressions.py  → exit 0 = green.
"""
from __future__ import annotations
import json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVE = str(REPO / "scripts" / "serve.py")

_fails = 0
def check(cond, msg):
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond: _fails += 1

STATE = Path(tempfile.mkdtemp(prefix="t857ui-"))
ENV = dict(os.environ,
           BOARD_REGISTRY=str(STATE / "registry.json"),
           BOARD_ASSIGNMENTS=str(STATE / "assignments.json"),
           BOARD_ACTIVE=str(STATE / "last-active"),
           BOARD_NO_AUTO_OPEN="1")
ENV.pop("CLAUDECODE", None)

def mk_board(name):
    root = Path(tempfile.mkdtemp(prefix=f"t857ui-{name}-"))
    bd = root / "board"; bd.mkdir()
    (bd / "board.json").write_text(json.dumps(
        {"title": name, "rev": 1, "cards": [], "columns": []}))
    return root, str(bd.resolve())

def main():
    # 12 boards → the chip can range from "+2 Boards" up to "+11 Boards".
    roots = [mk_board(f"Proj-{chr(65+i)}{i}") for i in range(12)]
    port = 7996
    assigns = {bd: 7900 + i for i, (_, bd) in enumerate(roots)}
    assigns[roots[0][1]] = port
    (STATE / "assignments.json").write_text(json.dumps(assigns))
    srv = subprocess.Popen([sys.executable, SERVE, "--project", str(roots[0][0])],
                           env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            sys.exit("test server never came up")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": 1800, "height": 900}).new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#project-tabs .proj-tab", timeout=15000)

            print("R7: chip never overhangs the nav across resize steps")
            overhangs = []
            # Shrink stepwise: each step's reflow must not trust the previous
            # (narrower) chip label. Cover the multi-tab → nearly-all-hidden range.
            for w in [1800, 1400, 1100, 900, 700, 560, 480, 420]:
                page.set_viewport_size({"width": w, "height": 900})
                page.wait_for_timeout(250)   # let the debounced reflow settle
                m = page.evaluate("""() => {
                    const nav  = document.querySelector('#project-tabs');
                    const wrap = nav && nav.querySelector('.proj-ovf-wrap');
                    if (!nav || !wrap || getComputedStyle(wrap).display === 'none')
                        return null;
                    const n = nav.getBoundingClientRect(), c = wrap.getBoundingClientRect();
                    const shown = [...nav.querySelectorAll('.proj-tab')]
                        .filter(t => t.style.display !== 'none').length;
                    return {navR: n.right, chipR: c.right, shown,
                            label: wrap.querySelector('.proj-overflow').textContent};
                }""")
                # Overhang is the accepted lesser evil ONLY when nothing more
                # can be hidden (just the active tab left). With >1 tab shown,
                # an overhang means the reflow kept a tab it had no room for.
                if m and m["shown"] > 1 and m["chipR"] > m["navR"] + 0.5:
                    overhangs.append((w, round(m["chipR"] - m["navR"], 1),
                                      m["label"], m["shown"]))
            check(not overhangs, f"no chip overhang at any width (got {overhangs})")

            print("R8: applyBoardTitle wipes rich-paste residue")
            r = page.evaluate("""() => {
                const el = document.querySelector('#board-title');
                const t = el.textContent;
                // The exact post-paste shape: residue whose textContent === t.
                el.innerHTML = '<span>' + t + '</span><br>';
                applyBoardTitle();
                return {nodes: el.childNodes.length,
                        elems: el.childElementCount, text: el.textContent, t};
            }""")
            check(r["elems"] == 0 and r["nodes"] <= 1,
                  f"residue normalized to a single text node (got {r['elems']} elems, {r['nodes']} nodes)")
            check(r["text"] == r["t"], "title text unchanged")
            browser.close()
    finally:
        srv.terminate()
        try: srv.wait(timeout=5)
        except Exception: srv.kill()

if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"{'PASS' if _fails == 0 else f'FAIL ({_fails})'}  ({time.time()-t0:.1f}s)")
    sys.exit(1 if _fails else 0)
