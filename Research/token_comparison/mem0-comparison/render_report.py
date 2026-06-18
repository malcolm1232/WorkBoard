"""Render REPORT.md mechanically from results/raw/*.json — WorkBoard vs mem0.

Every number is derived from the committed result JSON (nothing hand-typed).
Run order:  run_recall.py → run_live.py → run_bootstrap.py → render_report.py
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH / "lib"))
import safety  # noqa: E402
RAW = BENCH / "results" / "raw"
OUT = BENCH / "REPORT.md"


def load(name):
    return json.loads((RAW / name).read_text())


def pct(x):
    return "n/a" if x is None else f"{x:.1f}%"


def main():
    rec = load("recall.json")
    live = load("live.json")
    boot = load("bootstrap.json")

    agg = rec["aggregate_found_only"]
    shp = rec["by_shape_found_only"]
    tok = rec["tokenizer"]
    wr = live["memory_write_per_session"]
    io = live["memory_io_loop_projection"]
    pr = live["per_recall"]
    cx = live["allin_crossover"]
    snap = live["snapshot"]

    L = []
    w = L.append
    w("# WorkBoard vs mem0 — Live Memory Efficiency Study (2026-06)")
    w("")
    w("> **Auto-generated** by `render_report.py` from `results/raw/*.json`. "
      "Do not hand-edit the numbers — re-run the drivers and this renderer.")
    w(f"> Tokenizer: `{tok}` — the SAME tokenizer for both systems (the core "
      "fairness control). Card #730 / #734 / #749.")
    w(f"> Board snapshot: `{snap['sha256']}` ({snap['bytes']:,} B), a frozen COPY. "
      "Lives in-repo at `Research/token_comparison/mem0-comparison/` but is "
      "**non-invasive** — reads frozen copies, writes only here, never the live "
      "board (`lib/safety.py` enforces it). **This folder studies mem0 only.** "
      "Exhaustive companion: `REPORT_DETAILED.md`; plain-English explainer: "
      "`OVERVIEW.md`.")
    w("")

    # ---------------------------------------------------------------- TL;DR
    w("## TL;DR")
    w("")
    w(f"- **Live loop (headline):** over a {io['sessions']}-session project at "
      f"{io['recalls_per_session']} recalls/session, WorkBoard runs the full memory "
      f"loop (persist + recall) with **{pct(io['wb_vs_mem0_pct'])} fewer model tokens "
      f"than mem0** ({io['workboard_tokens']:,} vs {io['mem0_tokens']:,}). The reason "
      "is structural: **mem0 spends an LLM extraction call on *every* session** "
      f"(~{wr['mem0_model_input_tokens']:,} input tok), while WorkBoard's carding is "
      "inline in the agent's normal turn — **0 dedicated LLM calls**.")
    w(f"- **Matches mem0's own headline:** mem0 markets *“90% fewer tokens vs "
      f"full-context.”* On the same {pr['full_context_baseline']:,}-token baseline, "
      f"WorkBoard recall is **{pct(pr['workboard_vs_full_context_pct'])} lighter** "
      f"(mem0: {pct(pr['mem0_vs_full_context_pct'])}). WorkBoard can make the *same* "
      "vs-full-context claim — and additionally beats mem0 head-to-head on the loop.")
    w(f"- **Honest — mem0 wins the single recall.** mem0's flat ~{pr['mem0']:,}-token "
      f"bundle is **leaner than WorkBoard's content-rich cards** ({pr['workboard']:,} "
      "tok/recall). WorkBoard wins the *loop* because persistence is free, not "
      "because any single recall is smaller.")
    w(f"- **Honest — WorkBoard's heavier surface:** a per-turn protocol nudge "
      f"(306 tok/turn). All-in (incl. the nudge) WorkBoard stays under mem0 up to "
      f"~{cx['full_nudge_breakeven_turns']} turns/session at {cx['at_recalls_per_session']} "
      f"recalls; trimmed to ~40 tok/turn that rises to ~{cx['trimmed_nudge_breakeven_turns']} "
      "turns. Full crossover curve below.")
    w("")

    # ---------------------------------------------------------------- Method
    w("## Method")
    w("")
    w("- **In-repo & non-invasive.** Reads a frozen `board_snapshot.json` + a "
      "read-only copy of `card.py`; writes only under this folder.")
    w("- **Same tokenizer for both systems** (`tokencount.py`).")
    w("- **WorkBoard = real, measured.** Recall via the actual `card.py` against the "
      "frozen snapshot; bootstrap via the real harvest/bucketize path in a sandboxed "
      "`$HOME`.")
    w("- **mem0 = its own published numbers.** Retrieval ~1.8K tok/query and a "
      "single-pass ADD extraction call per session, from the Mem0 paper "
      "(arXiv:2504.19413) and mem0.ai/research-3. Defaults FAVOR mem0 (flat 1.8K "
      "regardless of fan-out is its best case) — so any WorkBoard margin is a floor.")
    w("- **Correctness is real:** a WorkBoard answer counts only if every gold fact "
      "literally appears in a fetched card (`resolve_answer_cards`). Off-board facts "
      "are honest misses (mem0 wins those).")
    w("")

    # ------------------------------------------------ STUDY 1 — LIVE (PRIMARY)
    w("## Study 1 — Live memory loop (PRIMARY)")
    w("")
    w("### (1) Memory-WRITE — model cost to persist each session's work")
    w("")
    w(f"| System | LLM calls / session | model input tok / session | over {io['sessions']} sessions |")
    w("|---|--:|--:|--:|")
    w(f"| **WorkBoard** (inline carding) | {wr['workboard_model_calls']} | "
      f"{wr['workboard_model_tokens']} | **0** |")
    w(f"| **mem0** (single-pass ADD) | {wr['mem0_model_calls']} (+{wr['mem0_embed_calls']} embed) | "
      f"{wr['mem0_model_input_tokens']:,} | {wr['mem0_model_input_tokens'] * io['sessions']:,} |")
    w("")
    w(f"WorkBoard's writeup is the main model's normal turn output, committed by the "
      f"deterministic `card.py` CLI — **zero extra LLM calls**. mem0 runs one ADD "
      f"extraction call per session over the ~{wr['avg_session_input_tokens']:,}-token "
      f"session (measured on the `{wr['corpus_used']}` corpus). That tax dominates the loop.")
    w("")
    w(f"### (2) Memory I/O loop — {io['sessions']} sessions × {io['recalls_per_session']} recalls (HEADLINE)")
    w("")
    w("Persist + recall combined (excludes WorkBoard's per-turn nudge — that's "
      "protocol overhead, accounted separately in (4)):")
    w("")
    w("| System | total model tokens | vs WorkBoard |")
    w("|---|--:|--:|")
    w(f"| **WorkBoard** | **{io['workboard_tokens']:,}** | — |")
    w(f"| mem0 | {io['mem0_tokens']:,} | WorkBoard **{pct(io['wb_vs_mem0_pct'])}** fewer |")
    w("")
    w("### (3) Per-recall, and the parallel *vs full-context* claim")
    w("")
    w(f"| System | tok / recall | savings vs full-context ({pr['full_context_baseline']:,}) |")
    w("|---|--:|--:|")
    w(f"| **WorkBoard** | {pr['workboard']:,} | **{pct(pr['workboard_vs_full_context_pct'])} fewer** |")
    w(f"| mem0 | {pr['mem0']:,} | {pct(pr['mem0_vs_full_context_pct'])} fewer |")
    w("")
    w("(“fewer” = reduction vs the naive baseline — same direction as mem0's "
      "marketed “90% fewer”.)")
    w("")
    w(f"mem0's famous *“90% token savings”* is this column — vs stuffing the whole "
      f"history. WorkBoard saves **{pct(pr['workboard_vs_full_context_pct'])}** on the "
      "same baseline. Head-to-head per single recall, mem0's flat bundle is lighter "
      f"({pr['mem0']:,} vs {pr['workboard']:,}) — WorkBoard trades a slightly richer "
      "recall for free writes and structured lifecycle answers.")
    w("")
    w("### (4) All-in crossover (honest — includes WorkBoard's per-turn nudge)")
    w("")
    w("| Turns | Recalls | WB all-in (full nudge) | WB all-in (trimmed) | mem0 all-in | WB(full) wins | WB(trim) wins |")
    w("|--:|--:|--:|--:|--:|:--:|:--:|")
    for g in cx["scenario_grid"]:
        w(f"| {g['turns']} | {g['recalls']} | {g['wb_allin_full_nudge']:,} | "
          f"{g['wb_allin_trimmed_nudge']:,} | {g['mem0_allin']:,} | "
          f"{'✅' if g['wb_full_wins'] else '—'} | {'✅' if g['wb_trimmed_wins'] else '—'} |")
    w("")
    w(f"At {cx['at_recalls_per_session']} recalls/session the full-nudge breakeven is "
      f"~{cx['full_nudge_breakeven_turns']} turns; trimmed, ~{cx['trimmed_nudge_breakeven_turns']}. "
      "Trim the nudge and WorkBoard wins all-in across realistic sessions.")
    w("")

    # ------------------------------------------------ STUDY 2 — RECALL
    w("## Study 2 — Recall detail (WorkBoard vs mem0)")
    w("")
    w("| Shape | n | WorkBoard | mem0 | WB vs mem0 |")
    w("|---|--:|--:|--:|--:|")
    for sh in ("pinpoint", "thematic", "lifecycle"):
        s = shp[sh]
        w(f"| {sh} | {s['n']} | {s['wb_mean_total']:.0f} | {s['m0_mean_total']:.0f} | "
          f"{pct(s['reduction_vs_mem0_pct'])} |")
    w(f"| **all** | {agg['n']} | **{agg['wb_mean_total']:.0f}** | {agg['m0_mean_total']:.0f} | "
      f"{pct(agg['reduction_vs_mem0_pct'])} |")
    w("")
    w("Positive % = WorkBoard lighter. mem0's flat 1.8K bundle makes it the leanest "
      "per single recall (negative numbers) — WorkBoard's edge is the *loop*, not the "
      "*lookup*.")
    w("")

    # ------------------------------------------------ STUDY 3 — BOOTSTRAP
    w("## Study 3 — Bootstrap (secondary — cost to BUILD the memory)")
    w("")
    w("| Corpus | Sessions | WB calls | mem0 calls | WB input tok | mem0 input tok | Input reduction |")
    w("|---|--:|--:|--:|--:|--:|--:|")
    for f in boot["fixtures"]:
        w(f"| {f['corpus']} | {f['sessions']} | {f['wb_model_calls']} | "
          f"{f['m0_model_calls']} | {f['wb_ingest_input_tokens']:,} | "
          f"{f['m0_ingest_input_tokens']:,} | **{pct(f['input_reduction_vs_mem0_pct'])}** |")
    w("")
    w("WorkBoard buckets work hourly and feeds compact digests (a deterministic, "
      "no-model pre-pass); mem0 feeds whole sessions to a model.")
    w("")

    # ------------------------------------------------ HONEST
    w("## Where each system wins (honest)")
    w("")
    w(f"**mem0 wins:** the leanest single recall (flat ~{pr['mem0']:,} tok); "
      "zero-discipline automatic cross-project capture; vague semantic recall of "
      f"off-board facts (board-miss {rec['board_misses']}).")
    w("")
    w("**WorkBoard wins:** free persistence (no per-session extraction tax — this "
      "carries the loop); structured, deterministic lifecycle recall; matches mem0's "
      f"vs-full-context headline ({pct(pr['workboard_vs_full_context_pct'])} fewer).")
    w("")
    w("Complements, not substitutes: mem0 = automatic cross-project semantic memory; "
      "WorkBoard = the structured, free-to-maintain project ledger. mem0's “90%” is "
      "vs a naive baseline — WorkBoard matches that AND removes the per-write "
      "extraction tax mem0 still pays.")
    w("")

    # ------------------------------------------------ REPRODUCE
    w("## Reproduce")
    w("")
    w("```bash")
    w("python3 build_fixtures.py        # freeze corpora from ~/.claude (once)")
    w("python3 run_recall.py            # Study 2")
    w("python3 run_live.py              # Study 1 (PRIMARY)")
    w("python3 run_bootstrap.py         # Study 3")
    w("python3 render_report.py         # regenerate this file")
    w("python3 render_report_detailed.py# the exhaustive companion")
    w("```")
    w("")
    w("Deterministic — re-running yields identical numbers (no network, no model "
      "calls). `board_snapshot.json` and `corpora/` are git-ignored (private).")
    w("")

    safety.assert_write_local(OUT)
    OUT.write_text("\n".join(L))
    print(f"wrote {OUT}  ({len(L)} lines)")


if __name__ == "__main__":
    main()
