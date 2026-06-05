#!/usr/bin/env python3
"""#327 single-HUD browser test.

Spins a real serve.py on a throwaway board + port, opens the REAL board.html in
chromium with its REAL EventSource, then POSTs the exact replay→speedup→reconcile
/progress sequence (the one that used to race) and asserts the HUD DOM:

  - the HUD never disappears (display:none) mid-flow,
  - the count is 1-based (first visible = 1/N, never 0/N),
  - each stage ends at N/N (not N-1/N — the "6/7 never 7/7" bug),
  - "✓ COMPLETE" appears exactly once, only AFTER the final emit,
  - the header transitions last-24h → speeding-up → reconciling.

No Haiku calls — we replay the event stream the pipeline would emit.
"""
import json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _post(url, payload):
    req = urllib.request.Request(url + "/progress", data=json.dumps(payload).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=3).read()


def _hud(page):
    return page.evaluate("""() => {
      const h = document.getElementById('load-hud');
      if (!h) return {present:false};
      const q = s => (h.querySelector(s)||{}).textContent || '';
      const cs = getComputedStyle(h);
      const vis = s => { const e = h.querySelector(s); return !!e && getComputedStyle(e).display !== 'none'; };
      // bar fill width as a fraction of the track — indeterminate sweep pins the
      // fill to ~35%, a "loaded" bar to ~100%.
      const fill = h.querySelector('#lh-fill'), track = h.querySelector('.lh-track');
      const fillRatio = (fill && track && track.offsetWidth) ? fill.offsetWidth / track.offsetWidth : 0;
      return {present:true, display: h.style.display, visible: cs.display !== 'none',
              done: q('#lh-done'), total: q('#lh-total'), pct: q('#lh-pct'),
              countVisible: vis('.lh-count'), pctVisible: vis('.lh-pct'),
              fillRatio,
              mode: q('#lh-mode'), status: q('.lh-status b'),
              window: q('#lh-window')};
    }""")


def main():
    from playwright.sync_api import sync_playwright

    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="hud_test_"))
    board_dir = tmp / "board"; board_dir.mkdir()
    shutil.copy(REPO / "templates" / "board.json", board_dir / "board.json")

    env = dict(os.environ, BOARD_PORT=str(port))
    env.pop("CLAUDECODE", None)
    srv_log = open(tmp / "server.log", "w")
    srv = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "serve.py"), "--port", str(port),
         "--board", str(board_dir / "board.json"), "--no-discover"],
        env=env, stdout=srv_log, stderr=subprocess.STDOUT)
    # serve.py resolves its OWN port (registry), so discover the actual one from
    # its startup banner rather than assuming our requested port stuck.
    import re
    url = None
    for _ in range(100):
        try:
            txt = (tmp / "server.log").read_text()
            m = re.search(r"http://127\.0\.0\.1:(\d+)", txt)
            if m:
                cand = f"http://127.0.0.1:{m.group(1)}"
                urllib.request.urlopen(cand, timeout=1)
                url = cand; break
        except Exception:
            pass
        time.sleep(0.1)
    if not url:
        srv.terminate(); srv_log.flush()
        raise SystemExit("server never came up\n--- server.log ---\n"
                         + (tmp / "server.log").read_text())

    failures, log = [], []

    def check(cond, msg):
        (log if cond else failures).append(("✓" if cond else "✗") + " " + msg)

    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            page = br.new_page()
            page.goto(url, wait_until="load")
            page.wait_for_timeout(700)  # let EventSource connect

            # Track every HUD state we observe + whether it ever hid mid-flow.
            ever_hidden = {"v": False}
            completes = {"n": 0}

            def emit(payload, settle=180):
                _post(url, payload)
                page.wait_for_timeout(settle)
                st = _hud(page)
                if st.get("present") and not st["visible"]:
                    ever_hidden["v"] = True
                if "COMPLETE" in (st.get("status") or ""):
                    completes["n"] += 1
                return st

            # ---- TIER 1: replay, 7 chunks (the "6/7" case) ----
            REP = 7
            first = emit({"done": 0, "total": REP, "phase": "replay",
                          "label": "staged — beginning extraction…"})
            check(first["visible"], "HUD visible at start")
            no_tail = page.evaluate("() => !document.getElementById('lh-tail')")
            check(no_tail, "no #lh-tail line (lean HUD — bottom no.3 removed)")
            check(first["done"].lstrip("0") == "1" or first["done"] == "1",
                  f"starts 1-based (got {first['done']}/{first['total']}, want 1/{REP})")

            SS = Path(os.path.expanduser("~/Desktop/ss"))
            SS.mkdir(parents=True, exist_ok=True)
            shots = []
            def shot(name):
                f = SS / f"hud_{name}.png"; page.screenshot(path=str(f)); shots.append(str(f))

            last_rep = first
            for done in range(1, REP + 1):
                if done == 3:
                    shot("1_replay_3of7")
                if done == REP:
                    # replay terminal: done==total, phase=replay, NO final → handoff
                    last_rep = emit({"done": REP, "total": REP, "phase": "replay",
                                     "label": "day-1 replayed in 6s · speeding up ▸▸"})
                else:
                    last_rep = emit({"done": done, "total": REP, "phase": "replay",
                                     "label": f"{done * 3} card(s) emitted so far"})
            check(last_rep["done"] == str(REP) and last_rep["total"] == str(REP),
                  f"replay ends at {REP}/{REP} (got {last_rep['done']}/{last_rep['total']})")
            check(last_rep["visible"], "HUD still visible after replay (handoff, no hide)")
            check("speeding up" in last_rep["mode"].lower()
                  or "SPEEDING" in last_rep["status"],
                  f"header advanced to speed-up (mode='{last_rep['mode']}')")
            shot("2_replay_handoff_7of7")

            # ---- TIER 2: speedup, 5 chunks ----
            SPD = 5
            sp_first = emit({"done": 0, "total": SPD, "phase": "speedup",
                             "label": "staged — beginning extraction…"})
            check(sp_first["visible"], "HUD visible entering speedup (no disappear between tiers)")
            last_sp = sp_first
            for done in range(1, SPD + 1):
                last_sp = emit({"done": done, "total": SPD, "phase": "speedup",
                                "label": f"{REP*3 + done*2} card(s) emitted so far"})
            # speedup terminal: done==total, phase=speedup, NO final → handoff to reconcile
            check(last_sp["done"] == str(SPD),
                  f"speedup ends at {SPD}/{SPD} (got {last_sp['done']}/{last_sp['total']})")
            check(last_sp["visible"], "HUD still visible after speedup")
            check("reconcil" in last_sp["mode"].lower()
                  or "RECONCIL" in last_sp["status"],
                  f"header advanced to reconcile (mode='{last_sp['mode']}', status='{last_sp['status']}')")
            check(completes["n"] == 0, f"no COMPLETE before final (saw {completes['n']})")

            # ---- RECONCILE: start (0/1) then final (1/1, final=true) ----
            rec0 = emit({"done": 0, "total": 1, "phase": "reconcile",
                         "label": "checking nothing's missed…"})
            check(rec0["visible"], "HUD visible during reconcile")
            # reconcile has no meaningful item count → the N/M + % must be HIDDEN
            # (was a stale 8/8 lag then a meaningless 1/1).
            check(not last_sp["countVisible"], "count hidden the instant we enter reconcile (no stale N/N)")
            check(not rec0["countVisible"] and not rec0["pctVisible"],
                  "no N/M and no % shown during reconcile sweep")
            # indeterminate "loading" bar (a sweeping segment), NOT a static full bar
            check(rec0["fillRatio"] < 0.6,
                  f"reconcile shows an indeterminate (partial) bar, not full (fillRatio={rec0['fillRatio']:.2f})")
            shot("3_reconcile")
            check(completes["n"] == 0, "still no COMPLETE during reconcile start")

            rec1 = emit({"done": 1, "total": 1, "phase": "reconcile", "final": True,
                         "label": "✓ 3 card(s) brought up to date"}, settle=250)
            shot("4_complete")
            check(not rec1["countVisible"] and not rec1["pctVisible"],
                  "reconcile completes number-free (no 1/1 on ✓ COMPLETE)")
            check(rec1["fillRatio"] > 0.9,
                  f"bar fully loads on reconcile completion (fillRatio={rec1['fillRatio']:.2f})")
            check("COMPLETE" in (rec1.get("status") or ""), "shows ✓ COMPLETE on final")
            check(completes["n"] == 1, f"COMPLETE appeared exactly once (saw {completes['n']})")
            check(not ever_hidden["v"], "HUD NEVER hid mid-flow (single coherent HUD)")

            # ---- auto-hide after final (6s) ----
            page.wait_for_timeout(6500)
            after = _hud(page)
            check(not after["visible"], "HUD auto-hides ~6s after final")

            br.close()
    finally:
        srv.terminate()
        try: srv.wait(timeout=5)
        except Exception: srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n".join(log))
    if failures:
        print("\n".join(failures))
        print(f"\nFAILED {len(failures)} check(s)")
        sys.exit(1)
    print(f"\n✓ ALL {len(log)} checks passed — single coherent HUD, 1-based, no race")


if __name__ == "__main__":
    main()
