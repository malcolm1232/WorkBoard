#!/usr/bin/env python3
"""e2e_workboard.py — reusable end-to-end test harness for board-steward.

Codifies the verification patterns proven during ARCH REDESIGN v2 (multi-board
routing + reconciliation) so future overhauls reuse the harness instead of
re-deriving throwaway test scripts each time.

THE CARDINAL RULE (every test obeys it):
  NEVER pollute the user's live board or global state.
  - State is isolated via env overrides: BOARD_REGISTRY / BOARD_ASSIGNMENTS /
    BOARD_ACTIVE point at temp files; BOARD_NO_SERVER forces direct file writes
    for tests that don't need a server.
  - Boards under test are throwaway temp dirs seeded from templates/board.json.
  - The live board (default :7891) card count is captured before the run and
    asserted UNCHANGED after — a regression that touches it fails the suite.
  - Everything temp is torn down at the end.

USAGE
  python3 e2e_workboard.py multiboard     # multi-board routing/isolation (no LLM, free)
  python3 e2e_workboard.py recon          # recon flag + gating (no LLM, free)
  python3 e2e_workboard.py recon-haiku    # real Haiku recon E2E (costs ~1-2 Haiku calls)
  python3 e2e_workboard.py all            # multiboard + recon (free tier)
  python3 e2e_workboard.py all --haiku    # everything incl. the Haiku E2E

  BOARD_REPO=/path/to/WorkBoard  overrides the repo (default ~/Desktop/WorkBoard)

EXTENDING (for the NEXT overhaul)
  Add a function `def test_<group>_<name>(ctx): ...` that uses ctx.board(),
  ctx.isolate(), ctx.assert_eq(...). Register it in GROUPS. Keep the cardinal
  rule: isolate state, use throwaway boards, never touch the live board.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(os.environ.get("BOARD_REPO", str(Path.home() / "Desktop" / "WorkBoard")))
SCRIPTS = REPO / "scripts"
TEMPLATE = REPO / "templates" / "board.json"
CARD_PY = SCRIPTS / "card.py"
LIVE_HEALTH = "http://127.0.0.1:7891/health"


# ───────────────────────── test framework ─────────────────────────

class Ctx:
    """Per-run context: tmp dirs, isolated env, pass/fail tally, cleanup."""

    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self._tmp: list[str] = []
        self._state = tempfile.mkdtemp(prefix="e2e-state-")
        self._tmp.append(self._state)
        # Isolate ALL cross-board state into temp files (never touch ~/.board-steward).
        os.environ["BOARD_REGISTRY"] = f"{self._state}/registry.json"
        os.environ["BOARD_ASSIGNMENTS"] = f"{self._state}/assignments.json"
        os.environ["BOARD_ACTIVE"] = f"{self._state}/last-active"
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))

    def board(self, cards: list[dict] | None = None, *, from_template: bool = False) -> Path:
        """Make a throwaway project with a board.json; return the board.json path."""
        proj = tempfile.mkdtemp(prefix="e2e-proj-")
        self._tmp.append(proj)
        bdir = Path(proj) / "board"
        bdir.mkdir()
        bj = bdir / "board.json"
        if from_template and TEMPLATE.exists():
            shutil.copy(TEMPLATE, bj)
            if cards is not None:
                d = json.loads(bj.read_text()); d["cards"] = cards
                bj.write_text(json.dumps(d))
        else:
            bj.write_text(json.dumps({
                "rev": 1, "nextNum": 99, "activeWorkId": None,
                "columns": [{"id": c, "name": c} for c in
                            ("task", "backlog", "inprogress", "done", "mandatory", "notes")],
                "cards": cards or [],
            }))
        return bj

    def card(self, num, column, title, *, tags=None, cid=None) -> dict:
        """A minimally-valid card dict (real cards always carry an `id`)."""
        return {"id": cid or f"c-{num}", "num": num, "column": column,
                "title": title, "tags": tags or [],
                "createdAt": datetime.now(timezone.utc).isoformat()}

    def assert_eq(self, name, got, want):
        if got == want:
            self.passed.append(name); print(f"  ✓ {name}")
        else:
            msg = f"got {got!r}, want {want!r}"
            self.failed.append((name, msg)); print(f"  ✗ {name} — {msg}")

    def assert_true(self, name, cond, detail=""):
        self.assert_eq(name, bool(cond), True) if cond else \
            (self.failed.append((name, detail)), print(f"  ✗ {name} — {detail}"))

    def cleanup(self):
        for d in self._tmp:
            shutil.rmtree(d, ignore_errors=True)


def live_board_cards() -> int | None:
    """Card count of the live :7891 board, or None if no server (both fine)."""
    try:
        import urllib.request
        with urllib.request.urlopen(LIVE_HEALTH, timeout=1) as r:
            return json.load(r).get("cards")
    except Exception:
        return None


# ───────────────────────── multi-board tests ─────────────────────────

def test_multiboard_routing_isolation(ctx: Ctx):
    """A card added from inside project A lands on A, never B; last-active tracks."""
    os.environ["BOARD_NO_SERVER"] = "1"  # direct file write, no server/probe
    A = ctx.board([]); B = ctx.board([])
    for bj, title in ((A, "card for A"), (B, "card for B")):
        subprocess.run([sys.executable, str(CARD_PY), "add", "--title", title,
                        "--column", "task"], cwd=str(bj.parent.parent),
                       capture_output=True, text=True, timeout=20)
    ta = [c["title"] for c in json.loads(A.read_text())["cards"]]
    tb = [c["title"] for c in json.loads(B.read_text())["cards"]]
    ctx.assert_eq("multiboard.routing: A isolated", ta, ["card for A"])
    ctx.assert_eq("multiboard.routing: B isolated", tb, ["card for B"])
    # #611 — last-active is now session-aware JSON; resolve via the API (the file
    # is no longer a bare path line). The two subprocess writes both update the
    # global pointer, so global resolves to the last mutated board (B).
    import importlib, port_registry as pr; importlib.reload(pr)
    active = pr.get_active()
    ctx.assert_eq("multiboard.last-active = last mutated (B)",
                  Path(active).resolve(), B.parent.resolve())
    os.environ.pop("BOARD_NO_SERVER", None)


def test_multiboard_disambiguation(ctx: Ctx):
    """At $HOME the active board wins over a newer-mtime board; mtime is fallback."""
    import importlib, port_registry as pr; importlib.reload(pr)
    A = ctx.board([]); B = ctx.board([])
    abd, bbd = str(A.parent.resolve()), str(B.parent.resolve())
    json.dump({abd: 7950, bbd: 7951}, open(os.environ["BOARD_ASSIGNMENTS"], "w"))
    pr.set_active(bbd)                                   # B active
    time.sleep(0.05); os.utime(A, None)                 # A newer mtime

    def pick():
        a = pr.get_active()
        if a and (Path(a) / "board.json").exists():
            return str(Path(a) / "board.json")
        best = None
        for d in pr.assignments():
            bj = Path(d) / "board.json"
            if bj.exists():
                m = bj.stat().st_mtime
                if best is None or m > best[0]:
                    best = (m, str(bj))
        return best[1] if best else ""

    ctx.assert_eq("multiboard.disambiguation: active (B) wins over mtime",
                  Path(pick()).resolve(), B.resolve())
    # #611 — session-aware: each session resolves ITS OWN board even though the
    # global pointer (used by pick()) was last set to B.
    pr.set_active(abd, "sess-A"); pr.set_active(bbd, "sess-B")
    ctx.assert_eq("multiboard.disambiguation: sess-A resolves A",
                  Path(pr.get_active("sess-A")).resolve(), A.parent.resolve())
    ctx.assert_eq("multiboard.disambiguation: sess-B resolves B",
                  Path(pr.get_active("sess-B")).resolve(), B.parent.resolve())
    ctx.assert_eq("multiboard.disambiguation: unseen session → global (B)",
                  Path(pr.get_active("sess-fresh")).resolve(), B.parent.resolve())
    os.remove(os.environ["BOARD_ACTIVE"])               # clear active → mtime fallback
    ctx.assert_eq("multiboard.disambiguation: mtime fallback (A)",
                  Path(pick()).resolve(), A.resolve())


def test_multiboard_concurrent_sessions(ctx: Ctx):
    """#611 regression guard — two sessions on different boards don't clobber each
    other's pointer. Each `card.py add` runs with a distinct CLAUDE_CODE_SESSION_ID;
    afterwards each session resolves ITS board, while the global pointer (an unseen
    session's fallback) is whichever ran last."""
    os.environ["BOARD_NO_SERVER"] = "1"
    A = ctx.board([]); B = ctx.board([])
    for bj, title, sid in ((A, "A1", "sess-A"), (B, "B1", "sess-B")):
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
        env.pop("BOARD_SKIP_DECOMPOSE_CHECK", None)  # don't collapse to the shared _auto slot
        subprocess.run([sys.executable, str(CARD_PY), "add", "--title", title,
                        "--column", "task"], cwd=str(bj.parent.parent),
                       capture_output=True, text=True, timeout=20, env=env)
    import importlib, port_registry as pr; importlib.reload(pr)
    ctx.assert_eq("multiboard.concurrent: sess-A → A (not clobbered by B)",
                  Path(pr.get_active("sess-A")).resolve(), A.parent.resolve())
    ctx.assert_eq("multiboard.concurrent: sess-B → B",
                  Path(pr.get_active("sess-B")).resolve(), B.parent.resolve())
    ctx.assert_eq("multiboard.concurrent: global → last writer (B)",
                  Path(pr.get_active()).resolve(), B.parent.resolve())
    os.environ.pop("BOARD_NO_SERVER", None)


# ───────────────────── concurrency stress (#633) ─────────────────────
# TRUE parallelism via real subprocesses. flock serializes across PROCESSES (not
# same-process threads), so every test spawns subprocesses; ThreadPoolExecutor
# just lets the parent block on each concurrently. BOARD_NO_SERVER=1 forces the
# direct-write path so writes contend on _boardio.board_lock (no server needed).

def _parallel(fns: list, max_workers: int | None = None) -> list:
    """Run thunks concurrently, return results in submission order."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers or len(fns)) as ex:
        return [f.result() for f in [ex.submit(fn) for fn in fns]]


def _add_proc(board: Path, title: str, sid: str):
    """A single `card.py add` as an independent session (own CLAUDE_CODE_SESSION_ID)."""
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid, BOARD_NO_SERVER="1")
    env.pop("BOARD_SKIP_DECOMPOSE_CHECK", None)  # keep distinct session slots (not _auto)
    return subprocess.run([sys.executable, str(CARD_PY), "add", "--board", str(board),
                           "--title", title, "--column", "task"],
                          capture_output=True, text=True, timeout=30, env=env).returncode


def test_concurrent_same_board(ctx: Ctx):
    """N sessions add to the SAME board at once → no lost update (CAS/flock holds)."""
    os.environ["BOARD_NO_SERVER"] = "1"
    N = 12
    bj = ctx.board([])
    base_rev = json.loads(bj.read_text())["rev"]
    rcs = _parallel([(lambda i=i: _add_proc(bj, f"concurrent card {i}", f"same-{i}"))
                     for i in range(N)])
    d = json.loads(bj.read_text())
    titles = [c["title"] for c in d["cards"]]
    ctx.assert_eq("concurrent.same: all procs ok", all(rc == 0 for rc in rcs), True)
    ctx.assert_eq("concurrent.same: no lost card (count==N)", len(d["cards"]), N)
    ctx.assert_eq("concurrent.same: titles unique (no dup/overwrite)",
                  len(set(titles)), N)
    ctx.assert_eq("concurrent.same: rev advanced by N", d["rev"] - base_rev, N)
    os.environ.pop("BOARD_NO_SERVER", None)


def test_concurrent_different_boards(ctx: Ctx):
    """N sessions each on its OWN board, in parallel → each card lands on its own
    board (no cross-pollution) and the #611 flock keeps EVERY session's last-active
    entry (no clobber under real concurrent set_active)."""
    os.environ["BOARD_NO_SERVER"] = "1"
    N = 8
    boards = [ctx.board([]) for _ in range(N)]
    rcs = _parallel([(lambda i=i: _add_proc(boards[i], f"own card {i}", f"diff-{i}"))
                     for i in range(N)])
    ctx.assert_eq("concurrent.diff: all procs ok", all(rc == 0 for rc in rcs), True)
    isolated = all(
        [c["title"] for c in json.loads(boards[i].read_text())["cards"]] == [f"own card {i}"]
        for i in range(N))
    ctx.assert_eq("concurrent.diff: each card on its own board only", isolated, True)
    import importlib, port_registry as pr; importlib.reload(pr)
    routed = {i: Path(pr.get_active(f"diff-{i}") or "/∅").resolve() == boards[i].parent.resolve()
              for i in range(N)}
    ctx.assert_eq("concurrent.diff: every session routes to its own board (flock held)",
                  all(routed.values()), True)


def test_concurrent_mixed(ctx: Ctx):
    """Mixed: several sessions share 2 boards while others are solo → per-board card
    counts exact and every session resolves the board it actually wrote."""
    os.environ["BOARD_NO_SERVER"] = "1"
    A = ctx.board([]); B = ctx.board([]); C = ctx.board([])
    # (board, session_id) work items: 3 on A, 2 on B, 1 on C — all at once.
    work = [(A, "mix-a0"), (A, "mix-a1"), (A, "mix-a2"),
            (B, "mix-b0"), (B, "mix-b1"), (C, "mix-c0")]
    rcs = _parallel([(lambda bj=bj, sid=sid: _add_proc(bj, f"{sid} card", sid))
                     for bj, sid in work])
    ctx.assert_eq("concurrent.mixed: all procs ok", all(rc == 0 for rc in rcs), True)
    ctx.assert_eq("concurrent.mixed: A has 3", len(json.loads(A.read_text())["cards"]), 3)
    ctx.assert_eq("concurrent.mixed: B has 2", len(json.loads(B.read_text())["cards"]), 2)
    ctx.assert_eq("concurrent.mixed: C has 1", len(json.loads(C.read_text())["cards"]), 1)
    import importlib, port_registry as pr; importlib.reload(pr)
    want = {sid: bj.parent.resolve() for bj, sid in work}
    routed = all(Path(pr.get_active(sid) or "/∅").resolve() == want[sid] for _, sid in work)
    ctx.assert_eq("concurrent.mixed: every session routes correctly", routed, True)


def test_session_key_consistency(ctx: Ctx):
    """#633 review — the WRITE key (_mark_active → set_active) and the READ key
    (find_board → get_active) must be the SAME identity, or automation paths
    (BOARD_SKIP_DECOMPOSE_CHECK=1 → '_auto') write under one key and read under
    another and routing silently falls to global. Exercise the real card.py write
    (which stamps last-active) then resolve via find_board's logic from $HOME."""
    import importlib, port_registry as pr; importlib.reload(pr)
    bj = ctx.board([])
    # Automation context: BOTH the write (card.py _mark_active) and a read must key '_auto'.
    env = dict(os.environ, BOARD_SKIP_DECOMPOSE_CHECK="1", BOARD_NO_SERVER="1")
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    subprocess.run([sys.executable, str(CARD_PY), "add", "--board", str(bj),
                    "--title", "auto card", "--column", "task"],
                   capture_output=True, text=True, timeout=20, env=env)
    # find_board reads via port_registry.session_id(); replicate that identity here.
    os.environ["BOARD_SKIP_DECOMPOSE_CHECK"] = "1"
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    ctx.assert_eq("session-key: identity is _auto under automation", pr.session_id(), "_auto")
    ctx.assert_eq("session-key: write key == read key (resolves own board, not global)",
                  Path(pr.get_active(pr.session_id()) or "/∅").resolve(), bj.parent.resolve())
    os.environ.pop("BOARD_SKIP_DECOMPOSE_CHECK", None)
    os.environ.pop("BOARD_NO_SERVER", None)


def test_concurrent_port_assign(ctx: Ctx):
    """#633 — N sessions first-touching N DISTINCT new boards concurrently must get
    N DISTINCT ports. Pre-fix the unlocked assign() TOCTOU collided them. Uses
    subprocesses (flock is cross-process, not cross-thread)."""
    N = 16
    projs = [tempfile.mkdtemp(prefix="e2e-port-") for _ in range(N)]
    ctx._tmp.extend(projs)
    def _assign(p):
        bd = str(Path(p) / "board")
        os.makedirs(bd, exist_ok=True)
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys;sys.path.insert(0,sys.argv[1]);import port_registry as pr;"
             "print(pr.assign(sys.argv[2]))", str(SCRIPTS), bd],
            capture_output=True, text=True, timeout=30, env=dict(os.environ)).stdout.strip()
        return int(out) if out.isdigit() else -1
    ports = _parallel([(lambda p=p: _assign(p)) for p in projs])
    ctx.assert_eq("concurrent.port: no assign failed", all(p > 0 for p in ports), True)
    ctx.assert_eq("concurrent.port: N distinct ports (no collision)",
                  len(set(ports)), N)


def test_concurrent_recon_pending(ctx: Ctx):
    """#633 — two sessions ending on the SAME board concurrently must not clobber
    each other's recon note; the merge unions both sessions' reasons."""
    bj = ctx.board([])
    bdir = bj.parent
    def _recon(sid, reason):
        payload = {"session_id": sid, "source": "stop_recon", "board": str(bj),
                   "reasons": [reason], "inprogress": [], "batched": []}
        return subprocess.run(
            [sys.executable, "-c",
             "import sys,json;sys.path.insert(0,sys.argv[1]);"
             "import _hook_stop_recon as h;from pathlib import Path;"
             "h._write_recon_pending(Path(sys.argv[2]), json.loads(sys.argv[3]))",
             str(SCRIPTS), str(bj), json.dumps(payload)],
            capture_output=True, text=True, timeout=30, env=dict(os.environ)).returncode
    _parallel([(lambda: _recon("recon-A", "reason from session A")),
               (lambda: _recon("recon-B", "reason from session B"))])
    rp = bdir / "recon_pending.json"
    ctx.assert_eq("concurrent.recon: file written", rp.exists(), True)
    merged = json.loads(rp.read_text()) if rp.exists() else {}
    reasons = merged.get("reasons", [])
    ctx.assert_eq("concurrent.recon: session A reason kept",
                  "reason from session A" in reasons, True)
    ctx.assert_eq("concurrent.recon: session B reason kept (no clobber)",
                  "reason from session B" in reasons, True)
    ctx.assert_eq("concurrent.recon: both sessions recorded",
                  set(merged.get("sessions", [])) == {"recon-A", "recon-B"}, True)


# ───────────────────────── recon tests (free) ─────────────────────────

def test_recon_only_discovered_flag(ctx: Ctx):
    """only_discovered scopes candidates: True→tagged only, False→all non-done."""
    os.environ["CLAUDECODE"] = "1"  # no-Haiku path: writes recon_pending we can count
    import importlib, hourly_reconcile as HR; importlib.reload(HR)
    bj = ctx.board([ctx.card(1, "inprogress", "live (untagged)"),
                    ctx.card(2, "inprogress", "mined", tags=["discovered"])])
    pend = bj.parent / "recon_pending.json"

    def n_candidates(only_disc):
        if pend.exists(): pend.unlink()
        HR.reconcile_sweep(CARD_PY, bj, [], only_discovered=only_disc)
        return len(json.loads(pend.read_text())["candidates"]) if pend.exists() else 0

    ctx.assert_eq("recon.only_discovered=True → mined only", n_candidates(True), 1)
    ctx.assert_eq("recon.only_discovered=False → all non-done", n_candidates(False), 2)
    os.environ.pop("CLAUDECODE", None)


def test_recon_gates_short_circuit(ctx: Ctx):
    """--reconcile-only must NOT call Haiku when (A) no non-done cards or
    (B) no recorded project activity (throwaway proj has none)."""
    def run(bj):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "hourly_extractor.py"),
             "--project", str(bj.parent.parent), "--board", str(bj),
             "--reconcile-only"], capture_output=True, text=True, timeout=30).stderr

    bjA = ctx.board([ctx.card(1, "done", "shipped")])
    ctx.assert_true("recon.gateA: only-done → skip", "no non-done cards" in run(bjA),
                    "expected 'no non-done cards' skip")
    bjB = ctx.board([ctx.card(1, "inprogress", "wip")])
    ctx.assert_true("recon.gateB: no project activity → skip",
                    "no recorded project activity" in run(bjB),
                    "expected 'no recorded project activity' skip")


def test_recon_replay_gate(ctx: Ctx):
    """#recon-after-replay: SessionStart recon-only must STAND DOWN while a
    bootstrap card-replay is streaming (completed_card_replay==0), and resume
    once it finishes (==1). A board with no replay state is default-open."""
    import importlib, hourly_extractor as HE; importlib.reload(HE)

    def run(bj):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "hourly_extractor.py"),
             "--project", str(bj.parent.parent), "--board", str(bj),
             "--reconcile-only"], capture_output=True, text=True, timeout=30).stderr

    GATE_MSG = "card replay in progress"
    # A non-done card so Gate A (no non-done cards) doesn't mask Gate 0.
    bj = ctx.board([ctx.card(1, "inprogress", "wip")])

    # Default-open: no .replay_state.json → gate must NOT fire.
    ctx.assert_true("recon.replay-gate: no state → default-open",
                    GATE_MSG not in run(bj), "gate fired without any replay state")

    # Replay in progress (flag 0) → gate fires, stands down.
    HE._mark_replay_started(bj, 2)
    ctx.assert_true("recon.replay-gate: in-progress (0) → skip",
                    GATE_MSG in run(bj), "gate did NOT stand down during replay")

    # Replay complete (flag 1) → gate opens; falls through to the later gates
    # (throwaway proj → Gate B 'no recorded project activity'), never the
    # replay-skip. Proves completed_card_replay==1 re-enables reconcile.
    HE._mark_replay_complete(bj)
    out = run(bj)
    ctx.assert_true("recon.replay-gate: complete (1) → proceeds",
                    GATE_MSG not in out, "gate still firing after replay completed")


def test_backfill_only_noop_healthy(ctx: Ctx):
    """#800: SessionStart now spawns `--backfill-only`, NOT `--reconcile-only`.
    On a healthy board (no partial-bootstrap state) it must be a fast no-op:
    the dropped-card recovery runs but finds nothing, and — critically — it must
    NEVER reconcile (no 'recon-only:' / 'moved N card(s)' output). This guards the
    real startup path the hook uses, so a regression that reintroduced reconcile
    at SessionStart would fail here."""
    def run(bj):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "hourly_extractor.py"),
             "--project", str(bj.parent.parent), "--board", str(bj),
             "--backfill-only"], capture_output=True, text=True, timeout=30)

    # A non-done card present, but no replay/partial state on disk → backfill has
    # nothing to recover and reconcile must not run.
    bj = ctx.board([ctx.card(1, "inprogress", "wip")])
    r = run(bj)
    ctx.assert_true("backfill-only: clean exit", r.returncode == 0,
                    f"non-zero exit: {r.returncode} / {r.stderr[:200]}")
    ctx.assert_true("backfill-only: never reconciles",
                    "recon-only:" not in r.stderr and "moved" not in r.stderr,
                    f"unexpected reconcile output: {r.stderr[:200]}")


def test_recon_claudecode_path(ctx: Ctx):
    """CLAUDECODE=1 → prose recon_pending (no Haiku). The unset→Haiku side is the
    recon-haiku E2E. This proves the spawn's `env -u CLAUDECODE` is load-bearing."""
    os.environ["CLAUDECODE"] = "1"
    import importlib, hourly_reconcile as HR; importlib.reload(HR)
    bj = ctx.board([ctx.card(1, "inprogress", "wip", tags=["discovered"])])
    HR.reconcile_sweep(CARD_PY, bj, [], only_discovered=True)
    ctx.assert_true("recon.CLAUDECODE=1 writes recon_pending (no Haiku)",
                    (bj.parent / "recon_pending.json").exists(),
                    "recon_pending.json not written")
    os.environ.pop("CLAUDECODE", None)


# ───────────────────────── recon test (real Haiku, opt-in) ─────────────────────────

def test_recon_haiku_e2e(ctx: Ctx):
    """Real Haiku: a shipped In-Progress card → done; a skipped one → backlog.
    Costs ~1 Haiku call. Throwaway board, CLAUDECODE unset, direct write."""
    os.environ.pop("CLAUDECODE", None)
    os.environ["BOARD_NO_SERVER"] = "1"
    import importlib, hourly_reconcile as HR; importlib.reload(HR)
    bj = ctx.board([ctx.card(1, "inprogress", "Add rate-limit header", cid="c-rate-1"),
                    ctx.card(2, "inprogress", "Investigate websocket spike", cid="c-spike-2")])
    now = datetime.now(timezone.utc)
    events = [
        {"kind": "user_prompt", "ts": now - timedelta(hours=2), "text": "add the rate-limit header to the API"},
        {"kind": "git_commit", "ts": now - timedelta(hours=1), "text": "add rate-limit header to API responses", "meta": {"shaShort": "abc1234"}},
        {"kind": "user_prompt", "ts": now - timedelta(minutes=30), "text": "great, shipped the rate limiter, it's done and verified"},
        {"kind": "user_prompt", "ts": now - timedelta(minutes=20), "text": "actually let's skip the websocket spike investigation for now, nvm"},
    ]
    print("    (calling real Haiku — may take ~5-30s)", file=sys.stderr)
    HR.reconcile_sweep(CARD_PY, bj, events, only_discovered=False)
    cols = {c["num"]: c["column"] for c in json.loads(bj.read_text())["cards"]}
    ctx.assert_eq("recon.haiku: shipped IP → done", cols.get(1), "done")
    ctx.assert_eq("recon.haiku: skipped IP → backlog", cols.get(2), "backlog")
    os.environ.pop("BOARD_NO_SERVER", None)


# ─────────────────── review-coverage backfill tests (free, #599) ───────────────────

def test_review_backfill_detect_extract(ctx: Ctx):
    """#599 — a /code-review SKILL in a turn is detected (namespace-stripped) and
    rides discover2 bucketing into the task record's `reviewed` field; a turn with
    no review skill stays unmarked (back-compat)."""
    import discover2_sources as S, discover2_extract as X
    # detector strips 'plugin:' and filters to review skills only
    o = {"message": {"content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": "plugin:code-review"}},
        {"type": "tool_use", "name": "Skill", "input": {"skill": "frontend-design"}},
    ]}}
    ctx.assert_eq("review.detect: namespace-strip + filter",
                  S.review_skills_from_tool_use(o), ["code-review"])
    # extraction: review skill in the task's own turns → record.reviewed
    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    evs = [
        {"ts": t0, "source": "jsonl", "kind": "user_prompt",
         "text": "review the card-drag freeze fix please", "files": [], "meta": {"cwd": "/p"}},
        {"ts": t0 + timedelta(seconds=30), "source": "jsonl", "kind": "asst_msg",
         "text": "running review", "files": ["/p/board.html"],
         "meta": {"tools": ["Skill"], "review_skills": ["code-review"]}},
    ]
    rec = X.task_to_record(X.extract_tasks(evs, 30, Path("/p"))[0], Path("/p"))
    ctx.assert_eq("review.extract: reviewed skill carried",
                  (rec.get("reviewed") or {}).get("skill"), "code-review")
    # back-compat: no review skill → reviewed is None
    evs2 = [
        {"ts": t0, "source": "jsonl", "kind": "user_prompt",
         "text": "build the thing for me right now please", "files": [], "meta": {"cwd": "/p"}},
        {"ts": t0 + timedelta(seconds=30), "source": "jsonl", "kind": "asst_msg",
         "text": "built", "files": ["/p/x.py"], "meta": {"tools": ["Edit"]}},
    ]
    rec2 = X.task_to_record(X.extract_tasks(evs2, 30, Path("/p"))[0], Path("/p"))
    ctx.assert_eq("review.extract: no-review → None", rec2.get("reviewed"), None)


def test_review_backfill_emit_stamp(ctx: Ctx):
    """#599 — emit_card stamps a mined card carrying `reviewed` via the shipped
    `card.py review` mechanism (reviewed tag + reviewedAt + 🔍 subtask); a card
    with no review stays in the pending-review coverage gap."""
    os.environ["BOARD_NO_SERVER"] = "1"
    import importlib, hourly_emit as E, card_commands as CC
    importlib.reload(E)
    bj = ctx.board([])
    reviewed_card = {"title": "Card-drag freeze fix", "column": "done", "priority": "mid",
                     "notes": "fixed the freeze", "reviewed": {"skill": "plugin:code-review"},
                     "_bucket_ts_iso": "2026-06-01T10:00:00+00:00"}
    plain_card = {"title": "Unrelated chore bump", "column": "done", "priority": "low",
                  "notes": "bumped version"}
    n1 = E.emit_card(CARD_PY, bj, reviewed_card, show_lifecycle=True, pace_s=0.0)
    n2 = E.emit_card(CARD_PY, bj, plain_card, show_lifecycle=True, pace_s=0.0)
    cards = {c["num"]: c for c in json.loads(bj.read_text())["cards"]}
    c1, c2 = cards.get(n1, {}), cards.get(n2, {})
    ctx.assert_true("review.emit: reviewed tag stamped", "reviewed" in (c1.get("tags") or []))
    ctx.assert_eq("review.emit: reviewedAt set", c1.get("reviewedAt"),
                  "2026-06-01T10:00:00+00:00")
    ctx.assert_true("review.emit: findings=[bootstrap]",
                    any(r.get("findings") == "[bootstrap]" for r in (c1.get("reviews") or [])))
    ctx.assert_true("review.emit: 🔍 subtask added",
                    any("🔍" in s.get("text", "") for s in (c1.get("subtasks") or [])))
    # coverage ledger: plain card is the gap, reviewed card is NOT
    ctx.assert_true("review.emit: plain card is pending-review", CC._is_pending_review(c2))
    ctx.assert_eq("review.emit: reviewed card not pending", CC._is_pending_review(c1), False)
    os.environ.pop("BOARD_NO_SERVER", None)


# ───────────────────────── runner ─────────────────────────

GROUPS = {
    "multiboard": [test_multiboard_routing_isolation, test_multiboard_disambiguation,
                   test_multiboard_concurrent_sessions],
    "concurrency": [test_concurrent_same_board, test_concurrent_different_boards,
                    test_concurrent_mixed, test_session_key_consistency,
                    test_concurrent_port_assign, test_concurrent_recon_pending],
    "recon": [test_recon_only_discovered_flag, test_recon_gates_short_circuit,
              test_recon_replay_gate, test_backfill_only_noop_healthy,
              test_recon_claudecode_path],
    "review-backfill": [test_review_backfill_detect_extract, test_review_backfill_emit_stamp],
    "recon-haiku": [test_recon_haiku_e2e],
}


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    group = args[0] if args else "all"

    if not CARD_PY.exists():
        print(f"error: board-steward repo not found at {REPO} "
              f"(set BOARD_REPO)", file=sys.stderr)
        return 2

    selected: list = []
    if group == "all":
        selected = (GROUPS["multiboard"] + GROUPS["concurrency"]
                    + GROUPS["recon"] + GROUPS["review-backfill"])
        if "--haiku" in flags:
            selected += GROUPS["recon-haiku"]
    elif group in GROUPS:
        selected = GROUPS[group]
    else:
        print(f"unknown group '{group}'. choose: {', '.join(GROUPS)}, all",
              file=sys.stderr)
        return 2

    live_before = live_board_cards()
    ctx = Ctx()
    print(f"\n=== board-steward e2e :: {group} "
          f"({'incl. ' if (group=='recon-haiku' or '--haiku' in flags) else 'no '}Haiku) ===")
    try:
        for fn in selected:
            print(f"\n• {fn.__name__}")
            try:
                fn(ctx)
            except Exception as e:  # a test that crashes is a failure, not a stop
                ctx.failed.append((fn.__name__, f"EXCEPTION: {e}"))
                print(f"  ✗ {fn.__name__} — EXCEPTION: {e}")
    finally:
        ctx.cleanup()

    # Cardinal rule: the live board must be untouched.
    live_after = live_board_cards()
    if live_before is not None:
        if live_after == live_before:
            print(f"\n  ✓ live board untouched ({live_before} cards before & after)")
        else:
            ctx.failed.append(("live-board-untouched",
                               f"{live_before} → {live_after} (POLLUTED!)"))
            print(f"\n  ✗ live board POLLUTED: {live_before} → {live_after}")

    print(f"\n=== {len(ctx.passed)} passed, {len(ctx.failed)} failed ===")
    for name, why in ctx.failed:
        print(f"  FAIL {name}: {why}")
    return 1 if ctx.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
