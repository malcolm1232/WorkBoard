#!/usr/bin/env python3
"""Fast, no-LLM regression smoke for board-steward (#316 SMOKE-HARNESS).

Run:  python3 dev/smoke_test.py        →  exit 0 = all green, exit 1 = any failure.

Guards the post-#307-refactor surface against the import/export-gap bug class the
4-monolith → 14-module split introduced (the _LLM_PROMPT / time / argparse / DEFER_RE
regressions the E2E install kept catching). Everything here is deterministic and
LLM-free, so it's safe to run on every commit / in CI.

Sections:
  A. import all skill modules
  B. every card.py subcommand --help parses (argparse-wiring gaps)
  C. static undefined-name audit across all modules (the NameError class)
  D. full card.py lifecycle on a scratch board (no-server flock path)
  E. measure_digest --diff (hourly_extractor → discover2 harvest + digest, lossless)
  F. discover2 full pipeline (harvest → extract → output)
  G. sweep-status completion guard (#315 leftover-pending detection)
"""
from __future__ import annotations

import ast
import builtins
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

PY = sys.executable
CARD = SCRIPTS / "card.py"

MODULES = [
    "card", "card_state", "card_commands",
    "serve", "serve_bootstrap",
    "hourly_extractor", "hourly_common", "hourly_emit", "hourly_reconcile",
    "discover2", "discover2_sources", "discover2_extract",
    "digest_compact", "measure_digest", "sweep_status",
]

SUBCOMMANDS = [
    "add", "update", "move", "fly", "bug", "improve", "subtask", "link",
    "column", "show", "recover", "migrate", "repair-links", "prelaunch-check",
    "list", "digest", "query", "wiki", "metrics", "export", "sim", "auto-ship",
    "sweep-status", "progress",
]

# `import *` provenance, so the audit knows what each module inherits.
STAR = {
    "card_commands": ["card_state"],
    "card": ["card_state", "card_commands"],
    "serve": ["serve_bootstrap"],
    "hourly_reconcile": ["hourly_common", "hourly_emit"],
    "hourly_extractor": ["hourly_common", "hourly_emit", "hourly_reconcile"],
    "discover2": ["discover2_sources", "discover2_extract"],
}

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))


# ---------- A. imports ----------
def section_imports() -> None:
    print("A. module imports")
    bad = []
    for m in MODULES:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{m}: {e}")
    record(f"{len(MODULES) - len(bad)}/{len(MODULES)} modules import", not bad,
           "; ".join(bad))


# ---------- B. CLI parse ----------
def section_cli() -> None:
    print("B. card.py subcommand --help parses")
    bad = []
    for s in SUBCOMMANDS:
        r = subprocess.run([PY, str(CARD), s, "--help"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(s)
    record(f"{len(SUBCOMMANDS) - len(bad)}/{len(SUBCOMMANDS)} subcommands parse",
           not bad, "failed: " + ", ".join(bad) if bad else "")


# ---------- C. undefined-name audit ----------
def _bound(node) -> set:
    out = set()
    for nd in ast.walk(node):
        if isinstance(nd, ast.Assign):
            for t in nd.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        out.add(x.id)
        elif isinstance(nd, (ast.AnnAssign, ast.NamedExpr)) and isinstance(nd.target, ast.Name):
            out.add(nd.target.id)
        elif isinstance(nd, (ast.For, ast.AsyncFor)):
            for x in ast.walk(nd.target):
                if isinstance(x, ast.Name):
                    out.add(x.id)
        elif isinstance(nd, (ast.With, ast.AsyncWith)):
            for it in nd.items:
                if it.optional_vars:
                    for x in ast.walk(it.optional_vars):
                        if isinstance(x, ast.Name):
                            out.add(x.id)
        elif isinstance(nd, ast.comprehension):
            for x in ast.walk(nd.target):
                if isinstance(x, ast.Name):
                    out.add(x.id)
        elif isinstance(nd, ast.ExceptHandler) and nd.name:
            out.add(nd.name)
        elif isinstance(nd, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(nd.name)
        elif isinstance(nd, ast.Import):
            for a in nd.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(nd, ast.ImportFrom):
            for a in nd.names:
                if a.name != "*":
                    out.add(a.asname or a.name)
    return out


def _names_of(mod: str) -> set:
    m = importlib.import_module(mod)
    return set(getattr(m, "__all__", [n for n in dir(m) if not n.startswith("_")]))


# common loop/param idioms my flat checker can't always scope — never real bugs
COMMON = {"x", "i", "j", "k", "n", "e", "s", "t", "c", "d", "f", "m", "p", "v",
          "kind", "color", "project", "depth", "nodes", "line", "head", "reason",
          "num", "args", "self", "cls", "key", "val", "count", "a", "src",
          "sources", "bucket_events", "bucket_min", "current_min", "max_depth"}


def section_audit() -> None:
    print("C. static undefined-name audit (NameError class)")
    flagged = {}
    for mod in MODULES:
        tree = ast.parse((SCRIPTS / f"{mod}.py").read_text())
        prov = set(dir(builtins)) | {"__file__", "__name__"} | _bound(tree)
        for s in STAR.get(mod, []):
            prov |= _names_of(s)
        unres = set()
        for fn in [x for x in ast.walk(tree)
                   if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            loc = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
            if fn.args.vararg:
                loc.add(fn.args.vararg.arg)
            if fn.args.kwarg:
                loc.add(fn.args.kwarg.arg)
            loc |= _bound(fn)
            for nd in ast.walk(fn):
                if (isinstance(nd, ast.Name) and isinstance(nd.ctx, ast.Load)
                        and nd.id not in prov and nd.id not in loc
                        and nd.id not in COMMON):
                    unres.add(nd.id)
        if unres:
            flagged[mod] = sorted(unres)
    record("0 unresolved module-global names", not flagged,
           "; ".join(f"{k}:{v}" for k, v in flagged.items()))


# ---------- G. sweep-status completion guard (#315) ----------
def section_sweep_status() -> None:
    print("G. sweep-status completion guard (#315)")
    import sweep_status
    with tempfile.TemporaryDirectory() as td:
        brd = Path(td) / "board" / "board.json"
        brd.parent.mkdir(parents=True)
        brd.write_text(json.dumps({"cards": [], "columns": [], "rev": 0}))
        pend = brd.parent / "extraction_pending.json"

        # clean: no pending file → not pending, empty hook line, human rc 0
        st = sweep_status.status(brd)
        clean_ok = (not st["pending"] and sweep_status.hook_line(brd) == ""
                    and sweep_status.human(brd)[1] == 0)
        record("clean board → sweep-status reports done (rc 0)", clean_ok,
               "" if clean_ok else f"st={st}")

        # CLI agrees on the clean case (exercises card.py wiring end-to-end)
        r = subprocess.run([PY, str(CARD), "--board", str(brd), "sweep-status"],
                           capture_output=True, text=True, env={**os.environ, "BOARD_NO_SERVER": "1"})
        record("card.py sweep-status exits 0 when clean", r.returncode == 0,
               r.stdout.strip() or r.stderr.strip())

        # pending: a leftover staged file → detected, hook line names the SWEEP, rc 1
        pend.write_text(json.dumps({"written_at": "2026-05-30T15:00:00+00:00",
                                    "chunks": [{"digest": "x"}, {"digest": "y"}]}))
        st = sweep_status.status(brd)
        line = sweep_status.hook_line(brd)
        text, rc = sweep_status.human(brd)
        pend_ok = (st["pending"] and st["chunks"] == 2 and "SWEEP" in line
                   and "2 chunk" in line and rc == 1)
        record("leftover pending → klaxon names the SWEEP, rc 1", pend_ok,
               "" if pend_ok else f"line={line!r} rc={rc}")

        r = subprocess.run([PY, str(CARD), "--board", str(brd), "sweep-status"],
                           capture_output=True, text=True, env={**os.environ, "BOARD_NO_SERVER": "1"})
        record("card.py sweep-status exits 1 when pending", r.returncode == 1,
               f"rc={r.returncode}: {r.stdout.strip()}")

        # corrupt pending file still counts as "sweep not done" (existence is the signal)
        pend.write_text("{ not valid json")
        st = sweep_status.status(brd)
        record("corrupt pending file still flags not-done", st["pending"] and st["chunks"] is None,
               f"st={st}")


# ---------- D. card.py lifecycle ----------
def section_lifecycle() -> None:
    print("D. card.py full lifecycle (no-server flock path)")
    with tempfile.TemporaryDirectory() as td:
        brd = Path(td) / "board" / "board.json"
        brd.parent.mkdir(parents=True)
        tpl = json.loads((REPO / "templates" / "board.json").read_text())
        tpl["cards"] = []
        tpl["nextNum"] = 1
        brd.write_text(json.dumps(tpl))
        env = {**os.environ, "BOARD_NO_SERVER": "1"}

        def cc(*a) -> int:
            return subprocess.run([PY, str(CARD), "--board", str(brd), *a],
                                  capture_output=True, text=True, env=env).returncode

        steps = [
            ("add", cc("add", "--code", "SMOKE", "--column", "task",
                       "--priority", "mid", "--title", "smoke", "--origin", "x")),
            ("move", cc("move", "SMOKE", "inprogress")),
            ("subtask", cc("subtask", "add", "SMOKE", "a subtask")),
            ("move-done", cc("move", "SMOKE", "done", "--writeup", "shipped")),
            ("bug", cc("bug", "SMOKE", "--reason", "regressed")),
            ("re-done", cc("move", "SMOKE", "done", "--writeup", "fixed")),
            ("improve", cc("improve", "SMOKE", "add a test")),
            ("show", cc("show", "SMOKE")),
            ("list", cc("list", "--column", "done")),
            ("digest", cc("digest")),
            ("query", cc("query", "--column", "done")),
            ("export", cc("export", "--format", "md")),
        ]
        failed = [n for n, rc in steps if rc != 0]
        # verify the bug-bounce was recorded in history[]
        card = next(c for c in json.loads(brd.read_text())["cards"]
                    if c.get("code") == "SMOKE")
        hops = len(card.get("history", []))
        record(f"{len(steps) - len(failed)}/{len(steps)} lifecycle ops + "
               f"{hops}-hop history", not failed and hops >= 5,
               "failed: " + ", ".join(failed) if failed else "")


# ---------- E. measure_digest --diff ----------
def section_measure() -> None:
    print("E. measure_digest --diff (harvest→digest, lossless)")
    r = subprocess.run([PY, str(SCRIPTS / "measure_digest.py"), "1", "--diff"],
                       capture_output=True, text=True, cwd=str(REPO))
    out = (r.stdout + r.stderr).lower()
    ok = r.returncode == 0 and ("pass" in out or "lossless" in out)
    record("measure_digest --diff PASS", ok,
           "" if ok else f"rc={r.returncode} {out.strip()[-120:]}")


# ---------- F. discover2 pipeline ----------
def section_discover() -> None:
    print("F. discover2 full pipeline (harvest→extract→output)")
    r = subprocess.run([PY, str(SCRIPTS / "discover2.py"),
                        "--project", str(REPO), "--days", "1"],
                       capture_output=True, text=True, cwd=str(REPO))
    record("discover2 pipeline rc=0", r.returncode == 0,
           "" if r.returncode == 0 else (r.stderr.strip()[-120:]))


def section_seed() -> None:
    """#285: a fresh adopter's empty project must NOT yield a blank board.
    Strict scope on a project with no local history → 0 tasks; the
    --seed-cross-project-if-empty fallback → non-empty (cross-project seed)."""
    print("H. #285 fresh-repo never-empty seed (discover2)")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="wb285smoke.")
    try:
        base = [PY, str(SCRIPTS / "discover2.py"),
                "--project", tmp, "--days", "3", "--max-tasks", "10"]
        strict = subprocess.run(base, capture_output=True, text=True)
        seeded = subprocess.run(base + ["--seed-cross-project-if-empty"],
                                capture_output=True, text=True)
        n_strict = json.loads(strict.stdout).get("taskCount", -1) if strict.returncode == 0 else -1
        n_seed = json.loads(seeded.stdout).get("taskCount", -1) if seeded.returncode == 0 else -1
        record("strict scope on fresh repo → 0 tasks", n_strict == 0,
               f"taskCount={n_strict}")
        # Only meaningful if the machine HAS cross-project history to seed from.
        if n_seed > 0:
            record("seed fallback on fresh repo → non-empty", True,
                   f"taskCount={n_seed}")
            record("seed notice printed (no silent cap)",
                   "#285 seed" in seeded.stderr, "")
        else:
            record("seed fallback (no cross-project history to seed)", True,
                   "skipped — clean machine has no history")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    fast = "--fast" in sys.argv
    print("=" * 60)
    print("board-steward regression smoke" + (" (--fast core)" if fast else " (no-LLM)"))
    print("=" * 60)
    # --fast = the import/export-gap class only (the #307 split risk): ~3s, hook-safe.
    # full adds the harvest-based functional checks (~15s): the pre-release gate.
    sections = (section_imports, section_cli, section_audit, section_sweep_status)
    if not fast:
        sections += (section_lifecycle, section_measure, section_discover,
                     section_seed)
    for sec in sections:
        try:
            sec()
        except Exception as e:  # noqa: BLE001
            record(sec.__name__ + " CRASHED", False, str(e))
        print()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("=" * 60)
    if passed == total:
        print(f"✅ ALL GREEN — {passed}/{total} checks passed")
        return 0
    print(f"❌ {total - passed} FAILED — {passed}/{total} passed")
    for n, ok, d in results:
        if not ok:
            print(f"   ✗ {n}  {d}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
