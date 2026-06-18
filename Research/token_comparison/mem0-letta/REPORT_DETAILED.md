# WorkBoard vs Letta · mem0 · claude-mem — DETAILED Efficiency Study

> **Auto-generated** by `render_report_detailed.py` from `results/raw/*.json`. Every number is derived; do not hand-edit — re-run the drivers + this renderer.
> Companion to the shorter `REPORT.md`. Card #730 / #734 / #735 / #738.

## 0. Provenance & fairness fingerprint

| Field | Value |
|---|---|
| Tokenizer (all systems) | `tiktoken-cl100k_base` |
| Board snapshot | `7c49f1314c6b87d4` (1,155,340 B) |
| Letta version | `0.16.8` (real measurement) |
| graphify version | `graphifyy 0.8.40` |
| WorkBoard recall | REAL — `card.py` against frozen snapshot |
| Location | `WorkBoard/graphify-comparison/` (in-repo, non-invasive) |

The single most important fairness control: **one tokenizer (`tiktoken-cl100k_base`) counts every token for every system.** It is the tokenizer the peers use for their own published figures, and it is documented to run ~10–15% *under* Claude's true tokenizer — so absolute token counts are if anything conservative, and the *ratios* (which is what we report) are tokenizer-invariant.

## 1. Executive summary — all peers

WorkBoard is compared head-to-head against three shipping memory systems, each measured on the SAME corpus with the SAME tokenizer. Peers are modeled from their OWN published numbers (mem0, claude-mem) or measured from their OWN shipped code (Letta) — never our guess of their internals.

| Peer | Their headline claim | Its baseline | WorkBoard head-to-head (live loop) | WorkBoard per-recall |
|---|---|---|--:|---|
| **mem0** | “90% fewer tokens, 91% lower latency” | full-context (not a peer) | **33.7% fewer** | heavier (2,399 vs 1,800) |
| **claude-mem** | “~95% / ~10× savings” | naive full-reload (not a peer) | **52.6% fewer** | lighter (2,399 vs 3,237) |
| **Letta** (MemGPT) | per-turn in-context memory | (n/a) | **81.0% fewer** (92.2% trimmed) | heavier (2,399 vs 1,064) |

**The one-sentence finding:** every peer markets a big number against a *naive baseline* (stuffing full context, or naive reload); **none reports a head-to-head against a structured work-ledger.** When you run that head-to-head on real history, WorkBoard runs the live memory loop with **33.7%–81.0% fewer model tokens** than the peers — because its writes are free and it carries no memory in context. It does **not** win every single recall (mem0 and Letta have leaner per-query retrieval); it wins the loop.

## 2. Definitions (what each number means)

- **Live loop** — the steady-state cost of working with a memory system ON: what it spends to *persist* each session's work (WRITE) plus what it injects to *recall* (READ), projected over a project lifetime.
- **Memory-WRITE** — model tokens/calls spent to store what happened. WorkBoard: 0 dedicated calls (the writeup is the agent's normal turn output, committed by `card.py`). mem0/claude-mem: one extraction/compression LLM call *per session*. Letta: an LLM tool-call *per write* + Haiku compaction.
- **Per-turn vs per-session** — mem0 & claude-mem pay once per session; **Letta pays every turn** (its memory blocks + tool schemas + system prompt are re-sent on every interaction). This is why Letta's loop cost is largest.
- **Recall** — tokens injected to answer one query. WorkBoard: real two-layer `card.py` retrieval (index grep + compact card). Peers: their published / shipped retrieval cost.
- **Full-context baseline** — the naive alternative of pasting the whole history each query (~26,000 tok). This is what mem0's “90%” and claude-mem's “95%” are measured against — NOT against each other.
- **All-in / crossover** — WorkBoard's one recurring tax is a per-turn protocol nudge (306 tok, trimmable to ~40). The crossover shows at what session length that tax erodes the loop advantage.

## 3. Method & fairness controls

1. **Same tokenizer** for all systems (`tokencount.py`).
2. **Same frozen corpus**, byte-fingerprinted (see §4). Excludes the 2026-06-11→15 inactivity gap so per-day numbers stay interpretable.
3. **Peers measured by their own evidence** — mem0 & claude-mem from published figures (citations in §10); Letta from its shipped system prompt + tool JSON schemas + `Memory.compile()`. Defaults FAVOR the peers (e.g. mem0's flat 1.8K recall regardless of fan-out is its best case).
4. **Gold answers pre-written** in `queries.json` before any system was queried.
5. **Correctness is real** — a WorkBoard answer counts only if every gold fact literally appears in a fetched card (`resolve_answer_cards` greedy set-cover). Off-board facts are honest misses, reported as peer wins.
6. **Non-invasive & deterministic** — reads frozen copies, writes only under this subfolder (`lib/safety.py`), and re-runs are byte-identical.

## 4. The corpus (frozen fixtures)

| Corpus | Window | Files | Bytes | Fingerprint | Sessions | Turns | Transcript tok |
|---|---|--:|--:|---|--:|--:|--:|
| tiny | 2026-06-16→2026-06-17 | 339 | 37,052,825 | `bd952ca8bc283a8e` | 339 | 2,302 | 1,496,394 |
| medium | 2026-05-28→2026-06-10 | 933 | 186,478,743 | `94c784a7c731351b` | 933 | 11,693 | 5,095,769 |

The live-loop numbers below use the **medium** corpus (avg session = 5,462 transcript tokens — the input each peer's per-session extraction must read).

## 5. Study 1 — Live loop vs mem0 & claude-mem

### 5.1 Memory-WRITE per session
| System | LLM calls/session | input tok/session | × 100 sessions |
|---|--:|--:|--:|
| WorkBoard (inline carding) | 0 | 0 | **0** |
| mem0 (single-pass ADD) | 1 (+1 embed) | 5,462 | 546,200 |
| claude-mem (SessionEnd compress) | 1 | 5,462 | 546,200 |

### 5.2 Memory I/O loop — 100 sessions × 3 recalls (HEADLINE)
| System | total model tokens | vs WorkBoard |
|---|--:|--:|
| **WorkBoard** | **719,700** | — |
| mem0 | 1,086,200 | WorkBoard **33.7%** fewer |
| claude-mem | 1,517,300 | WorkBoard **52.6%** fewer |

### 5.3 Per-recall + parallel vs-full-context claim
| System | tok/recall | vs full-context (26,000) |
|---|--:|--:|
| WorkBoard | 2,399 | **−90.8%** |
| mem0 | 1,800 | −93.1% |
| claude-mem | 3,237 | — |

WorkBoard matches mem0's famous “90%” on mem0's own baseline (−90.8% vs −93.1%). Head-to-head per single recall, mem0's flat bundle is leaner — WorkBoard wins the loop on free writes, not on recall size.

### 5.4 All-in crossover (FULL grid — honest)
Total session tokens incl. WorkBoard's per-turn nudge vs mem0's all-in:

| Turns | Recalls | WB (full nudge) | WB (trimmed) | mem0 all-in | WB-full wins | WB-trim wins |
|--:|--:|--:|--:|--:|:--:|:--:|
| 10 | 1 | 5,556 | 2,896 | 7,262 | ✅ | ✅ |
| 10 | 3 | 10,354 | 7,694 | 10,862 | ✅ | ✅ |
| 10 | 10 | 27,147 | 24,487 | 23,462 | — | — |
| 25 | 1 | 10,146 | 3,496 | 7,262 | — | ✅ |
| 25 | 3 | 14,944 | 8,294 | 10,862 | — | ✅ |
| 25 | 10 | 31,737 | 25,087 | 23,462 | — | — |
| 50 | 1 | 17,796 | 4,496 | 7,262 | — | ✅ |
| 50 | 3 | 22,594 | 9,294 | 10,862 | — | ✅ |
| 50 | 10 | 39,387 | 26,087 | 23,462 | — | — |
| 100 | 1 | 33,096 | 6,496 | 7,262 | — | ✅ |
| 100 | 3 | 37,894 | 11,294 | 10,862 | — | — |
| 100 | 10 | 54,687 | 28,087 | 23,462 | — | — |

Breakeven at 3 recalls/session: ~11.7 turns (full nudge), ~89.2 turns (trimmed).

## 6. Study 1b — Letta (MemGPT), REAL measurement

> Measured from Letta `0.16.8`'s own shipped artifacts (system prompt `memgpt_v2_chat`, generated tool JSON schemas, `Memory.compile()`), counted with the harness tokenizer. Server- and model-independent → reproduces wherever `pip install letta` works.

Letta's cost shape is **different in kind**: its memory machinery is re-sent **every turn**, not once per session.

### 6.1 Per-turn in-context overhead breakdown
| Component | tokens/turn | note |
|---|--:|---|
| MemGPT system prompt | 1,061 | `memgpt_v2_chat` |
| Core memory blocks (current fill) | 130 | persona+human blocks |
| Core memory blocks (full 5000-char capacity) | 1,975 | upper bound |
| All tool schemas | 2,253 | base + memory tools |
| …of which memory-tool schemas | 2,188 | the memory-attributable part |
| **Letta memory-attributable / turn** | **2,318** | blocks + memory-tool schemas |
| **Letta full in-context / turn** | **3,444** | + system prompt |
| WorkBoard / turn | 306 (trim 40) | nudge only; **0** memory carried |

Per-tool schema cost (real, from Letta's generated schemas):

| Tool | tokens |
|---|--:|
| `send_message` | 65 |
| `conversation_search` | 710 |
| `archival_memory_insert` | 287 |
| `archival_memory_search` | 464 |
| `memory_replace` | 486 |
| `memory_insert` | 241 |

### 6.2 Live loop — 100 sessions × 50 turns × 3 recalls
| System | total model tokens | vs Letta |
|---|--:|--:|
| **WorkBoard** (full nudge) | **2,259,400** | **81.0% fewer** |
| WorkBoard (trimmed nudge) | 929,400 | 92.2% fewer |
| Letta (memory-attributable) | 11,909,200 | — |
| Letta (full in-context) | 17,539,200 | WorkBoard 87.1% fewer |

### 6.3 Writes + real-server corroboration
- WorkBoard write: **0 model tokens/session** (deterministic `card.py`).
- Letta write: LLM memory tool calls (~2.5/session) + a Haiku compaction call when context fills.
- **Live cross-check:** a local Letta server (`ollama/ollama/llama3.2:3b  embed:ollama/ollama/nomic-embed-text:latest`) replaying real turns reported **~10,814 tokens/session** (2 sessions / 5 turns). Real per-turn usage *exceeds* the structural floor used above (the message buffer also grows) → **the headline is conservative.**
- **Honest — Letta wins:** per-recall ~1,064 tok < WorkBoard 2,399; and Letta is autonomous, self-editing, cross-session memory with zero carding discipline.

## 7. Study 2 — Recall (full 20-query detail)

| Shape | n | WorkBoard | mem0 | vs mem0 | claude-mem | vs cm |
|---|--:|--:|--:|--:|--:|--:|
| pinpoint | 6 | 2241 | 1800 | -24.5% | 2625 | 14.6% |
| thematic | 7 | 2134 | 1800 | -18.6% | 2893 | 26.2% |
| lifecycle | 6 | 2864 | 1800 | -59.1% | 4250 | 32.6% |
| **all** | 19 | **2399** | 1800 | -33.3% | 3237 | 25.9% |

Per-query (index/detail split for WorkBoard; peers are totals):

| Query | Shape | WB idx | WB detail | WB total | found | mem0 | claude-mem | answer cards |
|---|---|--:|--:|--:|:--:|--:|--:|---|
| P01 | pinpoint | 861 | 2536 | 3397 | ✓ | 1800 | 3000 | #627,#645,#646 |
| P02 | pinpoint | 835 | 1342 | 2177 | ✓ | 1800 | 3000 | #608,#609,#624 |
| P03 | pinpoint | 820 | 498 | 1318 | ✓ | 1800 | 1500 | #215 |
| P04 | pinpoint | 827 | 1607 | 2434 | ✓ | 1800 | 3000 | #74,#75,#454 |
| P05 | pinpoint | 357 | 1383 | 1740 | ✓ | 1800 | 1500 | #598 |
| P06 | pinpoint | 855 | 0 | 855 | miss | 1800 | 1500 |  |
| P07 | pinpoint | 762 | 1619 | 2381 | ✓ | 1800 | 3750 | #634,#635,#636,#637 |
| T01 | thematic | 816 | 2972 | 3788 | ✓ | 1800 | 3750 | #570,#627,#640,#645 |
| T02 | thematic | 796 | 1850 | 2646 | ✓ | 1800 | 3750 | #73,#74,#75,#454 |
| T03 | thematic | 840 | 894 | 1734 | ✓ | 1800 | 3750 | #494,#502,#503,#535 |
| T04 | thematic | 857 | 1112 | 1969 | ✓ | 1800 | 2250 | #443,#633 |
| T05 | thematic | 584 | 1522 | 2106 | ✓ | 1800 | 2250 | #563,#673 |
| T06 | thematic | 526 | 1009 | 1535 | ✓ | 1800 | 2250 | #299,#576 |
| T07 | thematic | 496 | 667 | 1163 | ✓ | 1800 | 2250 | #78,#503 |
| L01 | lifecycle | 828 | 4151 | 4979 | ✓ | 1800 | 7500 | #627,#639,#640,#641,#642,#643,#644,#645,#646 |
| L02 | lifecycle | 808 | 3128 | 3936 | ✓ | 1800 | 4500 | #608,#609,#610,#611,#624 |
| L03 | lifecycle | 829 | 1619 | 2448 | ✓ | 1800 | 3750 | #634,#635,#636,#637 |
| L04 | lifecycle | 868 | 1396 | 2264 | ✓ | 1800 | 4500 | #570,#572,#573,#576,#577 |
| L05 | lifecycle | 732 | 1087 | 1819 | ✓ | 1800 | 3000 | #103,#107,#384 |
| L06 | lifecycle | 806 | 933 | 1739 | ✓ | 1800 | 2250 | #626,#668 |

Board-misses (facts not on the board → honest peer wins): ['P06']. WorkBoard answered 19/20 queries.

## 8. Study 3 — Bootstrap (build cost, secondary)

| Corpus | Sessions | WB calls | mem0/cm calls | WB input tok | peer input tok | Reduction |
|---|--:|--:|--:|--:|--:|--:|
| tiny | 339 | 23 | 339 | 12,672 | 1,496,394 | **99.2%** |
| medium | 933 | 132 | 933 | 64,162 | 5,095,769 | **98.7%** |

WorkBoard buckets hourly and feeds compact digests (a deterministic, no-model pre-pass); the peers feed whole sessions to a model → 1–2 orders of magnitude more input tokens.

## 9. Bonus axis — graphify (code knowledge-graph) calibration

> graphify `graphifyy 0.8.40` builds a code knowledge-graph (710 nodes / 1396 edges / 34 communities over frozen copy of WorkBoard/scripts/*.py (37 files, 636KB)). Different domain (code structure, not work outcomes) — included as an always-on cost calibration.

| Surface | graphify | WorkBoard |
|---|--:|--:|
| Always-on / session | 61 | 97 |
| Per-prompt injection | 0 | 306 (trim 40) |
| SKILL.md (cold) | 8,207 | 5,898 |
| Query subgraph (mean) | 1,373 | 2,399 (card recall) |
| Write API tokens | 0 | 0 |
| Captures | code structure (entities + relationships) | work outcomes (what shipped / why / links / lifecycle) |

graphify has no per-prompt injection (no nudge); WorkBoard's recall is cheaper than graphify's subgraph query. Both write for 0 model tokens. They capture different things — graphify=code structure, WorkBoard=work outcomes — so this is a calibration, not a winner/loser axis.

## 10. Where each system wins (honest)

**WorkBoard wins:** free persistence (no per-session/per-turn extraction tax); carries 0 memory in context (board never auto-loaded); structured, deterministic, reproducible lifecycle recall; human-glanceable kanban; matches mem0's vs-full-context headline (−90.8%).

**Peers win:** mem0 — leanest single recall (flat ~1.8K) + zero-discipline cross-project capture. claude-mem — automatic conversational capture. Letta — autonomous self-editing memory, no carding discipline, lean archival recall. All three surface *vague semantic* facts that were never carded; WorkBoard's board simply doesn't hold off-board facts (board-miss ['P06']).

**The honest framing:** the peers' big marketing numbers are vs *naive baselines*; WorkBoard matches those AND removes the per-write tax the peers still pay. They are complements — peer = conversational/semantic memory, WorkBoard = the structured project ledger.

## 11. Limitations & threats to validity

- **mem0 & claude-mem are modeled, not run.** We use their *own* published per-op numbers (citations §12). Risk: their real deployment could differ — but we deliberately picked their best-case figures, so error favors them.
- **mem0's flat 1.8K recall** is held constant across query fan-out. For multi-fact lifecycle questions that is generous to mem0 (real retrieval might fetch more). Still, mem0 wins per-recall here — we did not tilt this our way.
- **The per-turn nudge is treated as protocol overhead** (excluded from the I/O-loop headline, included in the crossover). A skeptic who counts it as memory cost should read §5.4: WorkBoard then needs the trimmed nudge to win long sessions.
- **Single-user corpus** (one developer's real Claude-Code history). Ratios should generalize; absolute counts are corpus-specific.
- **tiktoken ≈ 10–15% under Claude's true tokenizer.** Applied equally to all systems, so ratios are unaffected.
- **Letta structural floor < real usage.** The live server reported higher per-turn tokens than the structural in-context floor we headline with → the Letta result is conservative.

## 12. Constants & sources (appendix)

**mem0** (`peers/mem0_adapter.py`) — arXiv:2504.19413 + mem0.ai/research-3:
- `recall_tokens_per_query` = 1800
- `recall_beam_long_context` = 6719
- `add_llm_calls_per_session` = 1
- `embed_calls_per_session` = 1
- `full_context_tokens_per_query` = 26000

**claude-mem** (`peers/claude_mem_adapter.py`) — claude-mem 13.6.1 README:
- `search_results` = 10
- `search_tok_per_result` = 75
- `search_rounds` = 1
- `detail_tok_per_unit` = 750
- `fragmentation` = 1.0

**Letta** (`peers/letta_adapter.py`, `letta_incontext_real.py`) — Letta `0.16.8` shipped code: system prompt `memgpt_v2_chat`, tools attached ['send_message', 'conversation_search', 'archival_memory_insert', 'archival_memory_search', 'memory_replace', 'memory_insert']. Per-tool schema tokens in §6.1.

## 13. Exact reproduction

```bash
cd WorkBoard/graphify-comparison
python3 build_fixtures.py        # freeze corpora from ~/.claude (once; reads only)
python3 run_recall.py            # Study 2 (3-way recall)
python3 run_live.py              # Study 1 (mem0 / claude-mem live loop)
python3 run_bootstrap.py         # Study 3 (build cost)
python3 render_report.py         # short REPORT.md
python3 render_report_detailed.py# this file
```

**Letta (Study 1b) — deterministic headline (no infra):**
```bash
python3.11 -m venv .letta-venv && .letta-venv/bin/pip install letta
.letta-venv/bin/python letta_incontext_real.py   # measures Letta's shipped artifacts
python3 run_live_letta.py                        # projects the live loop
```
**Letta live corroboration (optional, needs Docker + Ollama):** `docker run letta/letta` + `ollama pull llama3.2:3b`, then `letta_real_run.py`.

All inputs are frozen: `board_snapshot.json` (the exact board), `corpora/*/manifest.json` (fingerprinted transcripts), `results/raw/*.json` (every computed number). Re-runs are byte-identical. `board_snapshot.json`, `corpora/`, `.letta-venv/` are git-ignored (private/heavy); the code + `queries.json` ship so anyone can re-derive everything.
