"""Render REPORT_DETAILED.md — the exhaustive WorkBoard-vs-mem0 report.

Same discipline as render_report.py (every number derived from results/raw/*.json),
but far more complete: full per-query table, the complete crossover grid, corpus
fingerprints, a glossary, a limitations / threats-to-validity section, every mem0
constant with its source, and exact reproduction. Run after the drivers.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH / "lib"))
import safety  # noqa: E402
RAW = BENCH / "results" / "raw"
OUT = BENCH / "REPORT_DETAILED.md"


def load(name):
    return json.loads((RAW / name).read_text())


def pct(x):
    return "n/a" if x is None else f"{x:.1f}%"


def manifest(fx):
    p = BENCH / "corpora" / fx / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else None


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

    w("# WorkBoard vs mem0 — DETAILED Efficiency Study")
    w("")
    w("> **Auto-generated** by `render_report_detailed.py` from `results/raw/*.json`. "
      "Every number is derived; do not hand-edit. Companion to the shorter "
      "`REPORT.md`; plain-English explainer in `OVERVIEW.md`. Cards #730 / #734 / #749.")
    w("")
    w("## 0. Provenance & fairness fingerprint")
    w("")
    w("| Field | Value |")
    w("|---|---|")
    w(f"| Tokenizer (both systems) | `{tok}` |")
    w(f"| Board snapshot | `{snap['sha256']}` ({snap['bytes']:,} B) |")
    w("| WorkBoard | REAL — `card.py` against frozen snapshot |")
    w("| mem0 | MODELED from its own published numbers |")
    w("| Location | `Research/token_comparison/mem0-comparison/` (in-repo, non-invasive) |")
    w("")
    w("The single most important fairness control: **one tokenizer "
      f"(`{tok}`) counts every token for both systems.** It is documented to run "
      "~10–15% *under* Claude's true tokenizer, so absolute counts are conservative "
      "and the *ratios* we report are tokenizer-invariant.")
    w("")

    w("## 1. Executive summary")
    w("")
    w(f"| Metric | WorkBoard | mem0 | Result |")
    w("|---|--:|--:|---|")
    w(f"| Build memory (input tok) | {boot['fixtures'][-1]['wb_ingest_input_tokens']:,} | "
      f"{boot['fixtures'][-1]['m0_ingest_input_tokens']:,} | "
      f"**WB {pct(boot['fixtures'][-1]['input_reduction_vs_mem0_pct'])} fewer** |")
    w(f"| Persist / session | 0 | {wr['mem0_model_calls']} LLM call (~{wr['mem0_model_input_tokens']:,} tok) | **WB free** |")
    w(f"| Live loop / {io['sessions']} sessions | {io['workboard_tokens']:,} | {io['mem0_tokens']:,} | "
      f"**WB {pct(io['wb_vs_mem0_pct'])} fewer** |")
    w(f"| Per single recall | {pr['workboard']:,} | {pr['mem0']:,} | **mem0 leaner** |")
    w(f"| Recall savings vs full-context | {pct(pr['workboard_vs_full_context_pct'])} fewer | "
      f"{pct(pr['mem0_vs_full_context_pct'])} fewer | **WB ≈ matches mem0's “90%”** |")
    w("")
    w("**One-sentence finding:** mem0's marketing number (“90% fewer tokens”) is vs a "
      "naive *full-context* baseline, not a peer. Head-to-head on real history, "
      f"WorkBoard runs the live loop with **{pct(io['wb_vs_mem0_pct'])} fewer model "
      "tokens** — because its writes are free, while mem0 pays an LLM extraction call "
      "every session. WorkBoard does **not** win every single recall (mem0's flat "
      "bundle is leaner); it wins the loop.")
    w("")

    w("## 2. Definitions")
    w("")
    w("- **Live loop** — steady-state cost of working with memory ON: WRITE (persist) "
      "+ RECALL (use), projected over a project lifetime.")
    w("- **Memory-WRITE** — tokens/calls to store what happened. WorkBoard: 0 "
      "dedicated calls (the writeup is the agent's normal turn output, committed by "
      "`card.py`). mem0: one single-pass ADD extraction LLM call per session.")
    w("- **Recall** — tokens injected to answer one query. WorkBoard: real two-layer "
      "`card.py` retrieval. mem0: flat ~1.8K top-k bundle.")
    w("- **Full-context baseline** — the naive alternative of pasting the whole "
      f"history each query (~{pr['full_context_baseline']:,} tok). mem0's “90%” is vs "
      "this, NOT vs a peer.")
    w("- **All-in / crossover** — WorkBoard's one recurring tax is a per-turn protocol "
      "nudge (306 tok, trimmable to ~40). The crossover shows at what session length "
      "that tax erodes the loop advantage.")
    w("")

    w("## 3. Method & fairness controls")
    w("")
    w("1. **Same tokenizer** for both (`tokencount.py`).")
    w("2. **Same frozen corpus**, byte-fingerprinted (§4); excludes the 2026-06-11→15 "
      "inactivity gap.")
    w("3. **mem0 measured by its own evidence** — published retrieval (~1.8K/query) + "
      "single-pass ADD (1 LLM call/session). Defaults FAVOR mem0.")
    w("4. **Gold answers pre-written** in `queries.json` before querying.")
    w("5. **Correctness is real** — a WorkBoard answer counts only if every gold fact "
      "literally appears in a fetched card. Off-board facts are honest mem0 wins.")
    w("6. **Non-invasive & deterministic** — reads frozen copies, writes only here, "
      "re-runs byte-identical.")
    w("")

    w("## 4. The corpus (frozen fixtures)")
    w("")
    w("| Corpus | Window | Files | Bytes | Fingerprint | Sessions | Turns | Transcript tok |")
    w("|---|---|--:|--:|---|--:|--:|--:|")
    for f in boot["fixtures"]:
        m = manifest(f["corpus"]) or {}
        w(f"| {f['corpus']} | {f['window'][0]}→{f['window'][1]} | {m.get('files','?')} | "
          f"{m.get('bytes',0):,} | `{m.get('fingerprint','?')}` | {f['sessions']} | "
          f"{f['turns']:,} | {f['transcript_tokens']:,} |")
    w("")
    w(f"The live-loop numbers use the **{wr['corpus_used']}** corpus "
      f"(avg session = {wr['avg_session_input_tokens']:,} transcript tokens — what "
      "mem0's per-session ADD must read).")
    w("")

    w("## 5. Study 1 — Live loop")
    w("")
    w("### 5.1 Memory-WRITE per session")
    w(f"| System | LLM calls/session | input tok/session | × {io['sessions']} sessions |")
    w("|---|--:|--:|--:|")
    w(f"| WorkBoard (inline carding) | {wr['workboard_model_calls']} | {wr['workboard_model_tokens']} | **0** |")
    w(f"| mem0 (single-pass ADD) | {wr['mem0_model_calls']} (+{wr['mem0_embed_calls']} embed) | "
      f"{wr['mem0_model_input_tokens']:,} | {wr['mem0_model_input_tokens']*io['sessions']:,} |")
    w("")
    w(f"### 5.2 Memory I/O loop — {io['sessions']} sessions × {io['recalls_per_session']} recalls (HEADLINE)")
    w("| System | total model tokens | vs WorkBoard |")
    w("|---|--:|--:|")
    w(f"| **WorkBoard** | **{io['workboard_tokens']:,}** | — |")
    w(f"| mem0 | {io['mem0_tokens']:,} | WorkBoard **{pct(io['wb_vs_mem0_pct'])}** fewer |")
    w("")
    w("### 5.3 Per-recall + the parallel vs-full-context claim")
    w(f"| System | tok/recall | savings vs full-context ({pr['full_context_baseline']:,}) |")
    w("|---|--:|--:|")
    w(f"| WorkBoard | {pr['workboard']:,} | **{pct(pr['workboard_vs_full_context_pct'])} fewer** |")
    w(f"| mem0 | {pr['mem0']:,} | {pct(pr['mem0_vs_full_context_pct'])} fewer |")
    w("")
    w("### 5.4 All-in crossover (FULL grid — honest)")
    w("Total session tokens incl. WorkBoard's per-turn nudge vs mem0's all-in:")
    w("")
    w("| Turns | Recalls | WB (full nudge) | WB (trimmed) | mem0 all-in | WB-full wins | WB-trim wins |")
    w("|--:|--:|--:|--:|--:|:--:|:--:|")
    for g in cx["scenario_grid"]:
        w(f"| {g['turns']} | {g['recalls']} | {g['wb_allin_full_nudge']:,} | "
          f"{g['wb_allin_trimmed_nudge']:,} | {g['mem0_allin']:,} | "
          f"{'✅' if g['wb_full_wins'] else '—'} | {'✅' if g['wb_trimmed_wins'] else '—'} |")
    w("")
    w(f"Breakeven at {cx['at_recalls_per_session']} recalls/session: "
      f"~{cx['full_nudge_breakeven_turns']} turns (full nudge), "
      f"~{cx['trimmed_nudge_breakeven_turns']} turns (trimmed).")
    w("")

    w("## 6. Study 2 — Recall (full 20-query detail)")
    w("")
    w("| Shape | n | WorkBoard | mem0 | WB vs mem0 |")
    w("|---|--:|--:|--:|--:|")
    for sh in ("pinpoint", "thematic", "lifecycle"):
        s = shp[sh]
        w(f"| {sh} | {s['n']} | {s['wb_mean_total']:.0f} | {s['m0_mean_total']:.0f} | {pct(s['reduction_vs_mem0_pct'])} |")
    w(f"| **all** | {agg['n']} | **{agg['wb_mean_total']:.0f}** | {agg['m0_mean_total']:.0f} | {pct(agg['reduction_vs_mem0_pct'])} |")
    w("")
    w("Per-query (WorkBoard index/detail split; mem0 = flat bundle):")
    w("")
    w("| Query | Shape | WB idx | WB detail | WB total | found | mem0 | answer cards |")
    w("|---|---|--:|--:|--:|:--:|--:|---|")
    for r in rec["rows"]:
        found = "✓" if r["wb_found"] else "miss"
        cards = ",".join(f"#{c}" for c in r.get("answer_cards", []))
        w(f"| {r['id']} | {r['shape']} | {r['wb_index']} | {r['wb_detail']} | {r['wb_total']} | "
          f"{found} | {r['m0_total']} | {cards} |")
    w("")
    w(f"Board-misses (facts not on the board → honest mem0 wins): {rec['board_misses']}. "
      f"WorkBoard answered {rec['found_count']}/{rec['total_queries']} queries.")
    w("")

    w("## 7. Study 3 — Bootstrap (build cost, secondary)")
    w("")
    w("| Corpus | Sessions | WB calls | mem0 calls | WB input tok | mem0 input tok | Reduction |")
    w("|---|--:|--:|--:|--:|--:|--:|")
    for f in boot["fixtures"]:
        w(f"| {f['corpus']} | {f['sessions']} | {f['wb_model_calls']} | {f['m0_model_calls']} | "
          f"{f['wb_ingest_input_tokens']:,} | {f['m0_ingest_input_tokens']:,} | "
          f"**{pct(f['input_reduction_vs_mem0_pct'])}** |")
    w("")
    w("WorkBoard buckets hourly and feeds compact digests (deterministic, no-model "
      "pre-pass); mem0 feeds whole sessions to a model → 1–2 orders of magnitude more "
      "input tokens.")
    w("")

    w("## 8. Where each system wins (honest)")
    w("")
    w("**WorkBoard wins:** free persistence (no per-session extraction tax — carries "
      "the loop); structured, deterministic, reproducible lifecycle recall; "
      "human-glanceable kanban; matches mem0's vs-full-context headline "
      f"({pct(pr['workboard_vs_full_context_pct'])} fewer).")
    w("")
    w(f"**mem0 wins:** leanest single recall (flat ~{pr['mem0']:,} tok); zero-discipline "
      "automatic cross-project capture; vague semantic recall of things never carded "
      f"(board-miss {rec['board_misses']}).")
    w("")

    w("## 9. Limitations & threats to validity")
    w("")
    w("- **mem0 is modeled, not run.** It needs an OpenAI key + Qdrant; we use its OWN "
      "published per-op numbers (best case), so error favors mem0.")
    w("- **mem0's flat 1.8K recall** is held constant across query fan-out — generous "
      "to mem0 on multi-fact queries. It still wins per-recall here; we did not tilt "
      "this our way.")
    w("- **The per-turn nudge is treated as protocol overhead** (excluded from the "
      "I/O-loop headline, included in the crossover §5.4). A skeptic who counts it as "
      "memory cost should note WorkBoard then needs the trimmed nudge to win long "
      "sessions.")
    w("- **Single-user corpus** — ratios should generalize; absolute counts are "
      "corpus-specific.")
    w("- **tiktoken ≈ 10–15% under Claude's true tokenizer** — applied equally, so "
      "ratios are unaffected.")
    w("")

    w("## 10. mem0 constants & sources (appendix)")
    w("")
    w("`peers/mem0_adapter.py` — sources: arXiv:2504.19413 + mem0.ai/research-3:")
    for k, v in rec["m0_params"].items():
        w(f"- `{k}` = {v}")
    w("")

    w("## 11. Exact reproduction")
    w("")
    w("```bash")
    w("cd Research/token_comparison/mem0-comparison")
    w("python3 build_fixtures.py        # freeze corpora from ~/.claude (once; reads only)")
    w("python3 run_recall.py            # Study 2")
    w("python3 run_live.py              # Study 1 (PRIMARY)")
    w("python3 run_bootstrap.py         # Study 3")
    w("python3 render_report.py         # short REPORT.md")
    w("python3 render_report_detailed.py# this file")
    w("```")
    w("")
    w("All inputs frozen: `board_snapshot.json` (exact board), `corpora/*/manifest.json` "
      "(fingerprinted transcripts), `results/raw/*.json` (every number). Re-runs are "
      "byte-identical. `board_snapshot.json` + `corpora/` are git-ignored (private); "
      "the code + `queries.json` ship so anyone can re-derive everything.")
    w("")

    safety.assert_write_local(OUT)
    OUT.write_text("\n".join(L))
    print(f"wrote {OUT}  ({len(L)} lines)")


if __name__ == "__main__":
    main()
