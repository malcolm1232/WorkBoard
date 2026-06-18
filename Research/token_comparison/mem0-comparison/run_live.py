"""PRIMARY STUDY — Live memory loop: WorkBoard vs mem0 (steady-state cost).

Head-to-head, measured the SAME way on the SAME corpus — the apples-to-apples
number mem0's "90% vs full-context" marketing never reports.

Two model-token costs per working session:

  WRITE  (persist what happened)
    WorkBoard : inline carding — the writeup is the main model's normal turn
                output, committed via the deterministic `card.py` CLI.
                → 0 dedicated LLM calls, 0 dedicated input tokens.
    mem0      : one single-pass ADD extraction LLM call per session, reading the
                session messages (+1 embedding pass).            (arXiv:2504.19413)

  RECALL (use what was stored)
    WorkBoard : measured structured retrieval (index grep + compact card show).
    mem0      : flat ~1.8K injected memory bundle per query.       (mem0.ai/research-3)

WorkBoard's one HEAVIER surface is the per-turn protocol nudge (it injects a
reminder every turn; mem0 injects nothing until you query). The nudge is what
makes WRITE free, and it's trimmable to ~40 tok. We model the crossover honestly:
on pure memory I/O (write+recall) WorkBoard wins decisively; all-in with the FULL
nudge it only wins for shorter sessions or recall-heavy ones — trimmed, it wins
almost always. We publish the real curve, not a cherry-picked point.

Reads only the frozen snapshot + frozen corpora. Writes results/raw/live.json.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(BENCH_DIR / "peers"))
sys.path.insert(0, str(BENCH_DIR / "lib"))
import tokencount                    # noqa: E402
import corpus_stats as cs            # noqa: E402
import run_recall                    # noqa: E402
import mem0_adapter as m0            # noqa: E402
import safety                        # noqa: E402

RESULTS = BENCH_DIR / "results" / "raw"
CORPORA = BENCH_DIR / "corpora"

# --- MEASURED WorkBoard live injections (this machine, frozen snapshot) -------
WB_SESSIONSTART_DIGEST = 97     # `card.py --board <snapshot> digest`
WB_PER_TURN_NUDGE = 306         # scripts/hook_user_prompt.sh protocol nudge
WB_NUDGE_TRIMMED = 40           # TOKEN_BUDGET.md: trimmable to a one-liner
WB_WRITE_MODEL_CALLS = 0        # carding is inline CLI — no extra model call
WB_WRITE_MODEL_TOKENS = 0


def _avg_session_input_tokens() -> tuple[int, str]:
    """The messages mem0's add() reads per session = transcript tokens / sessions,
    measured on a real frozen corpus (prefer medium)."""
    for fx in ("medium", "large", "tiny"):
        cdir = CORPORA / fx
        if cdir.exists() and (cdir / "manifest.json").exists():
            s = cs.corpus_stats(cdir)
            if s["sessions"]:
                return round(s["transcript_tokens"] / s["sessions"]), fx
    raise SystemExit(
        "No corpus found. Run `python3 build_fixtures.py` first (needs ~/.claude)."
    )


def run(turns: int = 50, sessions_projection: int = 100, recalls_per_session: int = 3) -> dict:
    safety.assert_non_invasive()

    rec = run_recall.run()
    found = [r for r in rec["rows"] if r["wb_found"]]
    wb_recall = round(sum(r["wb_total"] for r in found) / len(found))
    m0_recall = round(sum(r["m0_total"] for r in found) / len(found))

    avg_in, corpus_used = _avg_session_input_tokens()
    m0_live = m0.live({"avg_session_input_tokens": avg_in})
    full_ctx = m0.DEFAULTS["full_context_tokens_per_query"]

    N, T, K = sessions_projection, turns, recalls_per_session

    # (1) MEMORY-WRITE per session — the clean structural head-to-head -----------
    write = {
        "workboard_model_calls": WB_WRITE_MODEL_CALLS,
        "workboard_model_tokens": WB_WRITE_MODEL_TOKENS,
        "mem0_model_calls": m0_live["write_model_calls"],
        "mem0_embed_calls": m0_live["write_embed_calls"],
        "mem0_model_input_tokens": m0_live["write_model_input_tokens"],
        "avg_session_input_tokens": avg_in,
        "corpus_used": corpus_used,
        "note": "WorkBoard cards inline (0 dedicated LLM calls); mem0 spends a "
                "single-pass ADD extraction call every session over the session "
                "messages (avg session input tokens).",
    }

    # (2) MEMORY I/O loop over a project lifetime (write + recall) — HEADLINE -----
    # Excludes WorkBoard's per-turn protocol nudge (carding-discipline overhead,
    # not memory I/O). The nudge is accounted for honestly in (4).
    wb_io = N * (WB_WRITE_MODEL_TOKENS + wb_recall * K)
    m0_io = N * (avg_in + m0_recall * K)
    io_loop = {
        "sessions": N, "recalls_per_session": K,
        "workboard_tokens": wb_io,
        "mem0_tokens": m0_io,
        "wb_vs_mem0_pct": round((1 - wb_io / m0_io) * 100, 1) if m0_io else None,
        "note": "memory I/O only (persist + recall); WorkBoard write is free, so "
                "mem0's per-session extraction tax dominates.",
    }

    # (3) PER-RECALL + the parallel 'vs full-context' claim (mem0's own baseline) -
    per_recall = {
        "workboard": wb_recall,
        "mem0": m0_recall,
        "wb_vs_mem0_pct": round((1 - wb_recall / m0_recall) * 100, 1),
        "full_context_baseline": full_ctx,
        "mem0_vs_full_context_pct": round((1 - m0_recall / full_ctx) * 100, 1),
        "workboard_vs_full_context_pct": round((1 - wb_recall / full_ctx) * 100, 1),
    }

    # (4) ALL-IN crossover (HONEST) — total session tokens incl. WB per-turn nudge
    def wb_allin(t, k, nudge):
        return WB_SESSIONSTART_DIGEST + nudge * t + wb_recall * k
    def m0_allin(k):
        return avg_in + m0_recall * k   # mem0: write add() + recalls, 0 per-turn
    grid = []
    for t in (10, 25, 50, 100):
        for k in (1, 3, 10):
            wf = wb_allin(t, k, WB_PER_TURN_NUDGE)
            wt = wb_allin(t, k, WB_NUDGE_TRIMMED)
            mm = m0_allin(k)
            grid.append({
                "turns": t, "recalls": k,
                "wb_allin_full_nudge": wf,
                "wb_allin_trimmed_nudge": wt,
                "mem0_allin": mm,
                "wb_full_wins": wf < mm,
                "wb_trimmed_wins": wt < mm,
            })
    cross_full = (avg_in + (m0_recall - wb_recall) * K - WB_SESSIONSTART_DIGEST) / WB_PER_TURN_NUDGE
    cross_trim = (avg_in + (m0_recall - wb_recall) * K - WB_SESSIONSTART_DIGEST) / WB_NUDGE_TRIMMED
    crossover = {
        "at_recalls_per_session": K,
        "full_nudge_breakeven_turns": round(cross_full, 1),
        "trimmed_nudge_breakeven_turns": round(cross_trim, 1),
        "note": "Below the breakeven turn count, WorkBoard's all-in session cost "
                "(incl. nudge) is still under mem0's. The nudge is WorkBoard's only "
                "heavier surface and is trimmable to ~40 tok.",
        "scenario_grid": grid,
    }

    out = {
        "tokenizer": tokencount.backend_name(),
        "snapshot": safety.snapshot_fingerprint(),
        "scenario": {"turns": T, "sessions": N, "recalls_per_session": K},
        "memory_write_per_session": write,
        "memory_io_loop_projection": io_loop,
        "per_recall": per_recall,
        "allin_crossover": crossover,
    }
    safety.assert_write_local(RESULTS)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "live.json").write_text(json.dumps(out, indent=2))
    return out


def _print(o):
    w = o["memory_write_per_session"]
    io = o["memory_io_loop_projection"]
    pr = o["per_recall"]
    cx = o["allin_crossover"]
    print("PRIMARY — Live memory loop (WorkBoard vs mem0)\n")
    print(f"snapshot {o['snapshot']['sha256']} ({o['snapshot']['bytes']:,} B) · "
          f"tokenizer {o['tokenizer']}\n")
    print("(1) MEMORY-WRITE per session (persist the work):")
    print(f"    WorkBoard : {w['workboard_model_calls']} LLM calls, "
          f"{w['workboard_model_tokens']} tokens   (inline carding)")
    print(f"    mem0      : {w['mem0_model_calls']} LLM call (+{w['mem0_embed_calls']} embed), "
          f"{w['mem0_model_input_tokens']:,} input tok   (single-pass ADD)")
    print(f"    [avg session = {w['avg_session_input_tokens']:,} tok, corpus={w['corpus_used']}]\n")
    print(f"(2) MEMORY I/O loop over {io['sessions']} sessions × {io['recalls_per_session']} recalls "
          f"— HEADLINE:")
    print(f"    WorkBoard {io['workboard_tokens']:,}  vs  mem0 {io['mem0_tokens']:,}  "
          f"→ WorkBoard {io['wb_vs_mem0_pct']}% fewer model tokens\n")
    print("(3) PER-RECALL:")
    print(f"    WorkBoard {pr['workboard']} | mem0 {pr['mem0']} ({pr['wb_vs_mem0_pct']}% vs mem0)")
    print(f"    vs full-context ({pr['full_context_baseline']:,}): "
          f"mem0 -{pr['mem0_vs_full_context_pct']}%, WorkBoard -{pr['workboard_vs_full_context_pct']}%\n")
    print("(4) ALL-IN crossover (honest — incl. WorkBoard per-turn nudge):")
    print(f"    at {cx['at_recalls_per_session']} recalls/session, WorkBoard's all-in cost "
          f"stays under mem0 up to ~{cx['full_nudge_breakeven_turns']} turns "
          f"(full nudge) / ~{cx['trimmed_nudge_breakeven_turns']} turns (trimmed).")


if __name__ == "__main__":
    _print(run())
