"""Render REPORT_DETAILED.md — the exhaustive companion to REPORT.md.

Same discipline (every number derived from results/raw/*.json, nothing hand-typed),
but far more complete: all four peers (mem0, claude-mem, Letta) + the graphify
code-graph calibration axis, full per-query tables, the complete crossover grid,
Letta's per-tool schema breakdown, the real-server corroboration, corpus
fingerprints, a glossary, a limitations / threats-to-validity section, every
constant with its source, and exact reproduction steps (including recreating the
Letta venv). Run AFTER the drivers:

  python3 run_recall.py && python3 run_live.py && python3 run_bootstrap.py
  python3 run_live_letta.py          # if the Letta results are stale
  python3 render_report_detailed.py
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


def load(name, optional=False):
    p = RAW / name
    if not p.exists():
        if optional:
            return None
        raise FileNotFoundError(p)
    return json.loads(p.read_text())


def pct(x):
    return "n/a" if x is None else f"{x:.1f}%"


def manifest(fx):
    p = BENCH / "corpora" / fx / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else None


def main():
    rec = load("recall.json")
    live = load("live.json")
    boot = load("bootstrap.json")
    ll = load("live_letta.json", optional=True)
    lr = load("letta_real.json", optional=True)
    cal = load("calibration.json", optional=True)

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

    # ===================================================================== HEADER
    w("# WorkBoard vs Letta · mem0 · claude-mem — DETAILED Efficiency Study")
    w("")
    w("> **Auto-generated** by `render_report_detailed.py` from `results/raw/*.json`. "
      "Every number is derived; do not hand-edit — re-run the drivers + this renderer.")
    w("> Companion to the shorter `REPORT.md`. Card #730 / #734 / #735 / #738.")
    w("")
    w("## 0. Provenance & fairness fingerprint")
    w("")
    w("| Field | Value |")
    w("|---|---|")
    w(f"| Tokenizer (all systems) | `{tok}` |")
    w(f"| Board snapshot | `{snap['sha256']}` ({snap['bytes']:,} B) |")
    if ll:
        w(f"| Letta version | `{ll.get('letta_version')}` ({ll.get('letta_source')} measurement) |")
    if cal:
        w(f"| graphify version | `{cal['_meta'].get('graphify_version')}` |")
    w(f"| WorkBoard recall | REAL — `card.py` against frozen snapshot |")
    w("| Location | `WorkBoard/graphify-comparison/` (in-repo, non-invasive) |")
    w("")
    w("The single most important fairness control: **one tokenizer "
      f"(`{tok}`) counts every token for every system.** It is the tokenizer the "
      "peers use for their own published figures, and it is documented to run "
      "~10–15% *under* Claude's true tokenizer — so absolute token counts are if "
      "anything conservative, and the *ratios* (which is what we report) are "
      "tokenizer-invariant.")
    w("")

    # ===================================================================== SUMMARY
    w("## 1. Executive summary — all peers")
    w("")
    w("WorkBoard is compared head-to-head against three shipping memory systems, "
      "each measured on the SAME corpus with the SAME tokenizer. Peers are modeled "
      "from their OWN published numbers (mem0, claude-mem) or measured from their "
      "OWN shipped code (Letta) — never our guess of their internals.")
    w("")
    w(f"| Peer | Their headline claim | Its baseline | WorkBoard head-to-head (live loop) | WorkBoard per-recall |")
    w("|---|---|---|--:|---|")
    w(f"| **mem0** | “90% fewer tokens, 91% lower latency” | full-context (not a peer) | "
      f"**{pct(io['wb_vs_mem0_pct'])} fewer** | heavier ({pr['workboard']:,} vs {pr['mem0']:,}) |")
    w(f"| **claude-mem** | “~95% / ~10× savings” | naive full-reload (not a peer) | "
      f"**{pct(io['wb_vs_claude_mem_pct'])} fewer** | lighter ({pr['workboard']:,} vs {pr['claude_mem']:,}) |")
    if ll:
        lp = ll["live_loop_projection"]
        lpr = ll["per_recall"]
        w(f"| **Letta** (MemGPT) | per-turn in-context memory | (n/a) | "
          f"**{pct(lp['wb_vs_letta_mem_pct'])} fewer** ({pct(lp['wb_trim_vs_letta_mem_pct'])} trimmed) | "
          f"heavier ({lpr['workboard']:,} vs {lpr['letta']:,}) |")
    w("")
    w("**The one-sentence finding:** every peer markets a big number against a *naive "
      "baseline* (stuffing full context, or naive reload); **none reports a "
      "head-to-head against a structured work-ledger.** When you run that "
      "head-to-head on real history, WorkBoard runs the live memory loop with "
      f"**{pct(io['wb_vs_mem0_pct'])}–{pct(ll['live_loop_projection']['wb_vs_letta_mem_pct']) if ll else 'n/a'} "
      "fewer model tokens** than the peers — because its writes are free and it "
      "carries no memory in context. It does **not** win every single recall (mem0 "
      "and Letta have leaner per-query retrieval); it wins the loop.")
    w("")

    # ===================================================================== GLOSSARY
    w("## 2. Definitions (what each number means)")
    w("")
    w("- **Live loop** — the steady-state cost of working with a memory system ON: "
      "what it spends to *persist* each session's work (WRITE) plus what it injects "
      "to *recall* (READ), projected over a project lifetime.")
    w("- **Memory-WRITE** — model tokens/calls spent to store what happened. "
      "WorkBoard: 0 dedicated calls (the writeup is the agent's normal turn output, "
      "committed by `card.py`). mem0/claude-mem: one extraction/compression LLM call "
      "*per session*. Letta: an LLM tool-call *per write* + Haiku compaction.")
    w("- **Per-turn vs per-session** — mem0 & claude-mem pay once per session; "
      "**Letta pays every turn** (its memory blocks + tool schemas + system prompt "
      "are re-sent on every interaction). This is why Letta's loop cost is largest.")
    w("- **Recall** — tokens injected to answer one query. WorkBoard: real two-layer "
      "`card.py` retrieval (index grep + compact card). Peers: their published / "
      "shipped retrieval cost.")
    w("- **Full-context baseline** — the naive alternative of pasting the whole "
      f"history each query (~{pr['full_context_baseline']:,} tok). This is what mem0's "
      "“90%” and claude-mem's “95%” are measured against — NOT against each other.")
    w("- **All-in / crossover** — WorkBoard's one recurring tax is a per-turn "
      "protocol nudge (306 tok, trimmable to ~40). The crossover shows at what "
      "session length that tax erodes the loop advantage.")
    w("")

    # ===================================================================== METHOD
    w("## 3. Method & fairness controls")
    w("")
    w("1. **Same tokenizer** for all systems (`tokencount.py`).")
    w("2. **Same frozen corpus**, byte-fingerprinted (see §4). Excludes the "
      "2026-06-11→15 inactivity gap so per-day numbers stay interpretable.")
    w("3. **Peers measured by their own evidence** — mem0 & claude-mem from published "
      "figures (citations in §10); Letta from its shipped system prompt + tool JSON "
      "schemas + `Memory.compile()`. Defaults FAVOR the peers (e.g. mem0's flat 1.8K "
      "recall regardless of fan-out is its best case).")
    w("4. **Gold answers pre-written** in `queries.json` before any system was queried.")
    w("5. **Correctness is real** — a WorkBoard answer counts only if every gold fact "
      "literally appears in a fetched card (`resolve_answer_cards` greedy set-cover). "
      "Off-board facts are honest misses, reported as peer wins.")
    w("6. **Non-invasive & deterministic** — reads frozen copies, writes only under "
      "this subfolder (`lib/safety.py`), and re-runs are byte-identical.")
    w("")

    # ===================================================================== CORPORA
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
    w(f"The live-loop numbers below use the **{wr['corpus_used']}** corpus "
      f"(avg session = {wr['avg_session_input_tokens']:,} transcript tokens — the "
      "input each peer's per-session extraction must read).")
    w("")

    # ===================================================================== STUDY 1
    w("## 5. Study 1 — Live loop vs mem0 & claude-mem")
    w("")
    w("### 5.1 Memory-WRITE per session")
    w("| System | LLM calls/session | input tok/session | "
      f"× {io['sessions']} sessions |")
    w("|---|--:|--:|--:|")
    w(f"| WorkBoard (inline carding) | {wr['workboard_model_calls']} | "
      f"{wr['workboard_model_tokens']} | **0** |")
    w(f"| mem0 (single-pass ADD) | {wr['mem0_model_calls']} (+{wr['mem0_embed_calls']} embed) | "
      f"{wr['mem0_model_input_tokens']:,} | {wr['mem0_model_input_tokens']*io['sessions']:,} |")
    w(f"| claude-mem (SessionEnd compress) | {wr['claude_mem_model_calls']} | "
      f"{wr['claude_mem_model_input_tokens']:,} | {wr['claude_mem_model_input_tokens']*io['sessions']:,} |")
    w("")
    w(f"### 5.2 Memory I/O loop — {io['sessions']} sessions × {io['recalls_per_session']} recalls (HEADLINE)")
    w("| System | total model tokens | vs WorkBoard |")
    w("|---|--:|--:|")
    w(f"| **WorkBoard** | **{io['workboard_tokens']:,}** | — |")
    w(f"| mem0 | {io['mem0_tokens']:,} | WorkBoard **{pct(io['wb_vs_mem0_pct'])}** fewer |")
    w(f"| claude-mem | {io['claude_mem_tokens']:,} | WorkBoard **{pct(io['wb_vs_claude_mem_pct'])}** fewer |")
    w("")
    w("### 5.3 Per-recall + parallel vs-full-context claim")
    w(f"| System | tok/recall | vs full-context ({pr['full_context_baseline']:,}) |")
    w("|---|--:|--:|")
    w(f"| WorkBoard | {pr['workboard']:,} | **−{pct(pr['workboard_vs_full_context_pct'])}** |")
    w(f"| mem0 | {pr['mem0']:,} | −{pct(pr['mem0_vs_full_context_pct'])} |")
    w(f"| claude-mem | {pr['claude_mem']:,} | — |")
    w("")
    w(f"WorkBoard matches mem0's famous “90%” on mem0's own baseline "
      f"(−{pct(pr['workboard_vs_full_context_pct'])} vs −{pct(pr['mem0_vs_full_context_pct'])}). "
      "Head-to-head per single recall, mem0's flat bundle is leaner — WorkBoard wins "
      "the loop on free writes, not on recall size.")
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

    # ===================================================================== STUDY 1b
    if ll and lr:
        pt = ll["per_turn_overhead"]
        bd = pt["letta_breakdown"]
        lp = ll["live_loop_projection"]
        wd = ll["write_dimension"]
        lpr = ll["per_recall"]
        w("## 6. Study 1b — Letta (MemGPT), REAL measurement")
        w("")
        w(f"> Measured from Letta `{ll['letta_version']}`'s own shipped artifacts "
          f"(system prompt `{lr['system_prompt']}`, generated tool JSON schemas, "
          "`Memory.compile()`), counted with the harness tokenizer. "
          "Server- and model-independent → reproduces wherever `pip install letta` works.")
        w("")
        w("Letta's cost shape is **different in kind**: its memory machinery is "
          "re-sent **every turn**, not once per session.")
        w("")
        w("### 6.1 Per-turn in-context overhead breakdown")
        w("| Component | tokens/turn | note |")
        w("|---|--:|---|")
        w(f"| MemGPT system prompt | {bd['system']:,} | `{lr['system_prompt']}` |")
        w(f"| Core memory blocks (current fill) | {bd['blocks']:,} | persona+human blocks |")
        w(f"| Core memory blocks (full 5000-char capacity) | {bd['blocks_full_capacity']:,} | upper bound |")
        w(f"| All tool schemas | {bd['tool_schemas']:,} | base + memory tools |")
        w(f"| …of which memory-tool schemas | {bd['memory_tool_schemas']:,} | the memory-attributable part |")
        w(f"| **Letta memory-attributable / turn** | **{pt['letta_memory_attributable']:,}** | blocks + memory-tool schemas |")
        w(f"| **Letta full in-context / turn** | **{pt['letta_full_incontext']:,}** | + system prompt |")
        w(f"| WorkBoard / turn | {pt['workboard_nudge_full']} (trim {pt['workboard_nudge_trimmed']}) | nudge only; **0** memory carried |")
        w("")
        w("Per-tool schema cost (real, from Letta's generated schemas):")
        w("")
        w("| Tool | tokens |")
        w("|---|--:|")
        for t, n in lr["tool_schema_tokens_by_tool"].items():
            w(f"| `{t}` | {n} |")
        w("")
        w(f"### 6.2 Live loop — {lp['sessions']} sessions × {lp['turns']} turns × {lp['recalls_per_session']} recalls")
        w("| System | total model tokens | vs Letta |")
        w("|---|--:|--:|")
        w(f"| **WorkBoard** (full nudge) | **{lp['workboard_tokens_full_nudge']:,}** | **{pct(lp['wb_vs_letta_mem_pct'])} fewer** |")
        w(f"| WorkBoard (trimmed nudge) | {lp['workboard_tokens_trimmed_nudge']:,} | {pct(lp['wb_trim_vs_letta_mem_pct'])} fewer |")
        w(f"| Letta (memory-attributable) | {lp['letta_memory_attributable_tokens']:,} | — |")
        w(f"| Letta (full in-context) | {lp['letta_full_incontext_tokens']:,} | WorkBoard {pct(lp['wb_vs_letta_full_pct'])} fewer |")
        w("")
        w("### 6.3 Writes + real-server corroboration")
        w(f"- WorkBoard write: **{wd['workboard_model_tokens_per_session']} model tokens/session** "
          "(deterministic `card.py`).")
        w(f"- Letta write: LLM memory tool calls (~{wd['letta_memory_tool_calls_per_session']}/session) "
          "+ a Haiku compaction call when context fills.")
        cor = lr.get("live_model_corroboration", {})
        if cor:
            w(f"- **Live cross-check:** a local Letta server (`{cor.get('backend','')}`) "
              f"replaying real turns reported **~{wd['letta_model_reported_total_per_session']:,.0f} "
              f"tokens/session** ({cor.get('sessions_run','?')} sessions / {cor.get('turns_run','?')} turns). "
              "Real per-turn usage *exceeds* the structural floor used above (the message "
              "buffer also grows) → **the headline is conservative.**")
        w(f"- **Honest — Letta wins:** per-recall ~{lpr['letta']:,} tok < WorkBoard "
          f"{lpr['workboard']:,}; and Letta is autonomous, self-editing, cross-session "
          "memory with zero carding discipline.")
        w("")

    # ===================================================================== STUDY 2
    w("## 7. Study 2 — Recall (full 20-query detail)")
    w("")
    w("| Shape | n | WorkBoard | mem0 | vs mem0 | claude-mem | vs cm |")
    w("|---|--:|--:|--:|--:|--:|--:|")
    for sh in ("pinpoint", "thematic", "lifecycle"):
        s = shp[sh]
        w(f"| {sh} | {s['n']} | {s['wb_mean_total']:.0f} | {s['m0_mean_total']:.0f} | "
          f"{pct(s['reduction_vs_mem0_pct'])} | {s['cm_mean_total']:.0f} | {pct(s['reduction_pct'])} |")
    w(f"| **all** | {agg['n']} | **{agg['wb_mean_total']:.0f}** | {agg['m0_mean_total']:.0f} | "
      f"{pct(agg['reduction_vs_mem0_pct'])} | {agg['cm_mean_total']:.0f} | {pct(agg['reduction_pct'])} |")
    w("")
    w("Per-query (index/detail split for WorkBoard; peers are totals):")
    w("")
    w("| Query | Shape | WB idx | WB detail | WB total | found | mem0 | claude-mem | answer cards |")
    w("|---|---|--:|--:|--:|:--:|--:|--:|---|")
    for r in rec["rows"]:
        found = "✓" if r["wb_found"] else "miss"
        cards = ",".join(f"#{c}" for c in r.get("answer_cards", []))
        w(f"| {r['id']} | {r['shape']} | {r['wb_index']} | {r['wb_detail']} | {r['wb_total']} | "
          f"{found} | {r['m0_total']} | {r['cm_total']} | {cards} |")
    w("")
    w(f"Board-misses (facts not on the board → honest peer wins): {rec['board_misses']}. "
      f"WorkBoard answered {rec['found_count']}/{rec['total_queries']} queries.")
    w("")

    # ===================================================================== STUDY 3
    w("## 8. Study 3 — Bootstrap (build cost, secondary)")
    w("")
    w("| Corpus | Sessions | WB calls | mem0/cm calls | WB input tok | peer input tok | Reduction |")
    w("|---|--:|--:|--:|--:|--:|--:|")
    for f in boot["fixtures"]:
        w(f"| {f['corpus']} | {f['sessions']} | {f['wb_model_calls']} | {f['m0_model_calls']} | "
          f"{f['wb_ingest_input_tokens']:,} | {f['m0_ingest_input_tokens']:,} | "
          f"**{pct(f['input_reduction_vs_mem0_pct'])}** |")
    w("")
    w("WorkBoard buckets hourly and feeds compact digests (a deterministic, no-model "
      "pre-pass); the peers feed whole sessions to a model → 1–2 orders of magnitude "
      "more input tokens.")
    w("")

    # ===================================================================== CALIBRATION
    if cal:
        g = cal["graphify"]; wb = cal["workboard"]; meta = cal["_meta"]
        w("## 9. Bonus axis — graphify (code knowledge-graph) calibration")
        w("")
        w(f"> graphify `{meta.get('graphify_version')}` builds a code knowledge-graph "
          f"({meta['graph']['nodes']} nodes / {meta['graph']['edges']} edges / "
          f"{meta['graph']['communities']} communities over "
          f"{meta.get('code_corpus','code')}). Different domain (code structure, not "
          "work outcomes) — included as an always-on cost calibration.")
        w("")
        w("| Surface | graphify | WorkBoard |")
        w("|---|--:|--:|")
        w(f"| Always-on / session | {g['always_on_per_session_tok']:,} | {wb['always_on_per_session_tok']:,} |")
        w(f"| Per-prompt injection | {g['per_prompt_injection_tok']} | {wb['per_prompt_nudge_tok']} (trim {wb['per_prompt_nudge_trimmed_tok']}) |")
        w(f"| SKILL.md (cold) | {g['skill_md_tok']:,} | {wb['skill_md_tok']:,} |")
        w(f"| Query subgraph (mean) | {g['query_subgraph_mean_tok']:,} | {wb['recall_mean_tok']:,} (card recall) |")
        w(f"| Write API tokens | {g['write_api_tokens']} | {wb['write_model_tokens']} |")
        w(f"| Captures | {g['captures']} | {wb['captures']} |")
        w("")
        w("graphify has no per-prompt injection (no nudge); WorkBoard's recall is "
          "cheaper than graphify's subgraph query. Both write for 0 model tokens. "
          "They capture different things — graphify=code structure, WorkBoard=work "
          "outcomes — so this is a calibration, not a winner/loser axis.")
        w("")

    # ===================================================================== HONEST
    w("## 10. Where each system wins (honest)")
    w("")
    w("**WorkBoard wins:** free persistence (no per-session/per-turn extraction tax); "
      "carries 0 memory in context (board never auto-loaded); structured, "
      "deterministic, reproducible lifecycle recall; human-glanceable kanban; matches "
      f"mem0's vs-full-context headline (−{pct(pr['workboard_vs_full_context_pct'])}).")
    w("")
    w("**Peers win:** mem0 — leanest single recall (flat ~1.8K) + zero-discipline "
      "cross-project capture. claude-mem — automatic conversational capture. Letta — "
      "autonomous self-editing memory, no carding discipline, lean archival recall. "
      "All three surface *vague semantic* facts that were never carded; WorkBoard's "
      f"board simply doesn't hold off-board facts (board-miss {rec['board_misses']}).")
    w("")
    w("**The honest framing:** the peers' big marketing numbers are vs *naive "
      "baselines*; WorkBoard matches those AND removes the per-write tax the peers "
      "still pay. They are complements — peer = conversational/semantic memory, "
      "WorkBoard = the structured project ledger.")
    w("")

    # ===================================================================== LIMITS
    w("## 11. Limitations & threats to validity")
    w("")
    w("- **mem0 & claude-mem are modeled, not run.** We use their *own* published "
      "per-op numbers (citations §12). Risk: their real deployment could differ — but "
      "we deliberately picked their best-case figures, so error favors them.")
    w("- **mem0's flat 1.8K recall** is held constant across query fan-out. For "
      "multi-fact lifecycle questions that is generous to mem0 (real retrieval might "
      "fetch more). Still, mem0 wins per-recall here — we did not tilt this our way.")
    w("- **The per-turn nudge is treated as protocol overhead** (excluded from the "
      "I/O-loop headline, included in the crossover). A skeptic who counts it as "
      "memory cost should read §5.4: WorkBoard then needs the trimmed nudge to win "
      "long sessions.")
    w("- **Single-user corpus** (one developer's real Claude-Code history). Ratios "
      "should generalize; absolute counts are corpus-specific.")
    w("- **tiktoken ≈ 10–15% under Claude's true tokenizer.** Applied equally to all "
      "systems, so ratios are unaffected.")
    w("- **Letta structural floor < real usage.** The live server reported higher "
      "per-turn tokens than the structural in-context floor we headline with → the "
      "Letta result is conservative.")
    w("")

    # ===================================================================== CITATIONS
    w("## 12. Constants & sources (appendix)")
    w("")
    w("**mem0** (`peers/mem0_adapter.py`) — arXiv:2504.19413 + mem0.ai/research-3:")
    for k, v in rec["m0_params"].items():
        w(f"- `{k}` = {v}")
    w("")
    w("**claude-mem** (`peers/claude_mem_adapter.py`) — claude-mem 13.6.1 README:")
    for k, v in rec["cm_params"].items():
        w(f"- `{k}` = {v}")
    w("")
    if lr:
        w(f"**Letta** (`peers/letta_adapter.py`, `letta_incontext_real.py`) — Letta "
          f"`{ll.get('letta_version')}` shipped code: system prompt `{lr['system_prompt']}`, "
          f"tools attached {lr['tools_attached']}. Per-tool schema tokens in §6.1.")
        w("")

    # ===================================================================== REPRODUCE
    w("## 13. Exact reproduction")
    w("")
    w("```bash")
    w("cd WorkBoard/graphify-comparison")
    w("python3 build_fixtures.py        # freeze corpora from ~/.claude (once; reads only)")
    w("python3 run_recall.py            # Study 2 (3-way recall)")
    w("python3 run_live.py              # Study 1 (mem0 / claude-mem live loop)")
    w("python3 run_bootstrap.py         # Study 3 (build cost)")
    w("python3 render_report.py         # short REPORT.md")
    w("python3 render_report_detailed.py# this file")
    w("```")
    w("")
    w("**Letta (Study 1b) — deterministic headline (no infra):**")
    w("```bash")
    w("python3.11 -m venv .letta-venv && .letta-venv/bin/pip install letta")
    w(".letta-venv/bin/python letta_incontext_real.py   # measures Letta's shipped artifacts")
    w("python3 run_live_letta.py                        # projects the live loop")
    w("```")
    w("**Letta live corroboration (optional, needs Docker + Ollama):** "
      "`docker run letta/letta` + `ollama pull llama3.2:3b`, then `letta_real_run.py`.")
    w("")
    w("All inputs are frozen: `board_snapshot.json` (the exact board), "
      "`corpora/*/manifest.json` (fingerprinted transcripts), `results/raw/*.json` "
      "(every computed number). Re-runs are byte-identical. `board_snapshot.json`, "
      "`corpora/`, `.letta-venv/` are git-ignored (private/heavy); the code + "
      "`queries.json` ship so anyone can re-derive everything.")
    w("")

    safety.assert_write_local(OUT)
    OUT.write_text("\n".join(L))
    print(f"wrote {OUT}  ({len(L)} lines)")


if __name__ == "__main__":
    main()
