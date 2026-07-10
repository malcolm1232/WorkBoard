#!/usr/bin/env python3
"""#866 — the absolutely-centered board title overlaps the right control
cluster (Reset Columns / Sort Time / Tags / view-toggle) in the ~860-1100px
band: #731's <860px breakpoint drops the title into flex flow, but above it
nothing clamps the title's width, so it slides under the buttons ("the middle
overlaps" when the window is half-minimized).

Contract: in absolute-centered mode the title wrap's right edge must stay
clear of every visible header control at every width; a too-long title
ellipsizes instead of colliding. Reuses the #857 harness (isolated boards,
real serve.py, Playwright).

Run: python3 dev/test_866_title_clamp.py  → exit 0 = green.
"""
from __future__ import annotations
import json, subprocess, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_857_ui_regressions as h  # harness: mk_board / STATE / ENV / SERVE

_fails = 0
def check(cond, msg):
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond: _fails += 1

MEASURE = """() => {
    const twrap = document.querySelector('header .title-wrap');
    if (!twrap) return null;
    const mode = getComputedStyle(twrap).position;   // absolute=centered, static=in-flow
    const t = twrap.getBoundingClientRect();
    const hits = [];
    for (const sel of ['#cols-pill', '#tags-btn', '#view-toggle',
                       '#project-tabs .proj-tab.active',
                       '#project-tabs .proj-ovf-wrap']) {
        const el = document.querySelector(sel);
        if (!el || getComputedStyle(el).display === 'none') continue;
        if (mode !== 'absolute' && sel.startsWith('#project-tabs')) continue;  // in-flow shares the row
        const r = el.getBoundingClientRect();
        const overlap = Math.min(t.right, r.right) - Math.max(t.left, r.left);
        if (overlap > 0.5) hits.push({sel, overlap: Math.round(overlap)});
    }
    const h1 = twrap.querySelector('h1');
    // The overflow chip must never be CLIPPED by a squeezed nav either
    // (overflow:hidden cuts it mid-label, e.g. "+11 B|").
    let navClip = 0;
    const nav = document.querySelector('#project-tabs');
    const ovf = nav && nav.querySelector('.proj-ovf-wrap');
    if (nav && ovf && getComputedStyle(nav).display !== 'none'
        && getComputedStyle(ovf).display !== 'none') {
        navClip = Math.max(0, Math.round(
            ovf.getBoundingClientRect().right - nav.getBoundingClientRect().right));
    }
    // Control buttons must stay fully visible — a squeezed pill silently
    // clipping Sort Time is a functional loss, not just cosmetic.
    let ctrlClip = 0;
    const sortBtn = document.querySelector('#sort-time-btn');
    const pill = document.querySelector('#cols-pill');
    if (sortBtn && pill && getComputedStyle(pill).display !== 'none') {
        ctrlClip = Math.max(0, Math.round(
            sortBtn.getBoundingClientRect().right - pill.getBoundingClientRect().right));
    }
    return {hits, titleW: t.width, mode, navClip, ctrlClip,
            clipped: h1.scrollWidth > h1.clientWidth + 1};
}"""

def main():
    # A deliberately long title — the worst case for the centered band.
    root, bd = h.mk_board("Kaggle-Predict-1Y-Stock-Returns-Board")
    port = 7996
    (h.STATE / "assignments.json").write_text(json.dumps({bd: port}))
    srv = subprocess.Popen([sys.executable, h.SERVE, "--project", str(root)],
                           env=h.ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); break
            except Exception:
                time.sleep(0.2)
        else:
            sys.exit("test server never came up")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": 1400, "height": 600}).new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#board-title", timeout=15000)
            page.wait_for_timeout(400)   # controls (pills/toggle) finish showing

            print("T1: centered title never overlaps visible right controls")
            collisions = []
            for w in [1400, 1300, 1200, 1100, 1050, 1000, 950, 900, 870]:
                page.set_viewport_size({"width": w, "height": 600})
                page.wait_for_timeout(250)
                m = page.evaluate(MEASURE)
                if m and m["hits"]:
                    collisions.append((w, m["hits"]))
            check(not collisions, f"no title/control overlap at any width (got {collisions})")

            print("T2: long title ellipsizes rather than colliding at 1000px")
            page.set_viewport_size({"width": 1000, "height": 600})
            page.wait_for_timeout(250)
            m = page.evaluate(MEASURE)
            check(bool(m) and not m["hits"], "clear of controls at 1000px")
            check(bool(m) and (m["clipped"] or m["titleW"] < 420),
                  f"title truncated/ellipsized to fit (w={m and round(m['titleW'])}, clipped={m and m['clipped']})")

            print("T3: wide screens keep the roomy centered title")
            page.set_viewport_size({"width": 1800, "height": 600})
            page.wait_for_timeout(250)
            m = page.evaluate(MEASURE)
            check(bool(m) and not m["clipped"], "no needless truncation at 1800px")
            browser.close()
    finally:
        srv.terminate()
        try: srv.wait(timeout=5)
        except Exception: srv.kill()

    # T4 — MULTI-board: a long ACTIVE tab + chip on the left must not slide
    # under the centered title either (the tabs' lesser-evil overhang used to
    # run beneath it).
    roots = [h.mk_board("Kaggle-Predict-1Y-Stock-Returns-Board")] + \
            [h.mk_board(f"Proj-{chr(66+i)}") for i in range(11)]
    port = 7996
    assigns = {bd: 7900 + i for i, (_, bd) in enumerate(roots)}
    assigns[roots[0][1]] = port
    (h.STATE / "assignments.json").write_text(json.dumps(assigns))
    srv = subprocess.Popen([sys.executable, h.SERVE, "--project", str(roots[0][0])],
                           env=h.ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); break
            except Exception:
                time.sleep(0.2)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": 1800, "height": 600}).new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#project-tabs .proj-tab", timeout=15000)
            page.wait_for_timeout(400)
            print("T4: multi-board — title clear of long active tab + chip too")
            collisions = []
            # Mix small steps AND big jumps (1400→1000, 870→1300): the settle
            # once converged on small steps but left overlap on jumpy paths.
            clips = []
            for w in [1400, 1200, 1100, 1000, 950, 900, 870, 1300, 1000, 900]:
                page.set_viewport_size({"width": w, "height": 600})
                page.wait_for_timeout(250)
                m = page.evaluate(MEASURE)
                if m and m["hits"]:
                    collisions.append((w, m["hits"]))
                if m and m["navClip"] > 1:
                    clips.append((w, "nav", m["navClip"]))
                if m and m["ctrlClip"] > 1:
                    clips.append((w, "ctrl", m["ctrlClip"]))
            check(not collisions, f"no overlap incl. tabs at any width (got {collisions})")
            check(not clips, f"chip and control buttons never clipped (got {clips})")

            print("T5: growing back to 1800px restores the centered title")
            page.set_viewport_size({"width": 1800, "height": 600})
            page.wait_for_timeout(300)
            m = page.evaluate(MEASURE)
            check(bool(m) and m["mode"] == "absolute" and not m["hits"],
                  f"centered again with no overlap (mode={m and m['mode']})")
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
