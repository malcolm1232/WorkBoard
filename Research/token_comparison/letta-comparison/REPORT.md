# WorkBoard vs Letta, mem0 & claude-mem — Live Memory Efficiency Study (2026-06)

> **Auto-generated** by `render_report.py` from `results/raw/*.json`. Do not hand-edit the numbers — re-run the drivers and this renderer.
> Tokenizer: `tiktoken-cl100k_base` — the SAME tokenizer applied to all three systems (the core fairness control). Card #730.
> Board snapshot: `7c49f1314c6b87d4` (1,155,340 B), a frozen COPY. This study lives **inside the repo at `WorkBoard/Research/token_comparison/letta-comparison/`** as a tracked sub-project, but is **non-invasive**: it reads frozen copies (`board_snapshot.json`, `lib/product_scripts_ro/`) and writes ONLY under this subfolder — never the live board (`board/board.json`) or product source elsewhere (`lib/safety.py` enforces it). A more exhaustive companion report is in `REPORT_DETAILED.md`.

## TL;DR

- **Live loop (the headline):** over a 100-session project at 3 recalls/session, WorkBoard runs the full memory loop (persist + recall) with **33.7% fewer model tokens than mem0** (719,700 vs 1,086,200) — and 52.6% fewer than claude-mem. The reason is structural: **mem0 spends an LLM extraction call on *every* session** (~5,462 input tok), while WorkBoard's carding is inline in the agent's normal turn — **0 dedicated LLM calls**.
- **Matches mem0's own headline:** mem0 markets *“90% fewer tokens vs full-context.”* On the same 26,000-token full-context baseline, WorkBoard recall is **90.8% lighter** (mem0: 93.1%). So WorkBoard can make the *same* vs-full-context claim mem0 does — and additionally beats mem0 head-to-head on the write-heavy live loop.
- **Honest — where mem0 wins:** mem0's per-query retrieval is a flat ~1,800-token bundle, which is **leaner than WorkBoard's content-rich cards** (2,399 tok/recall). mem0's whole selling point — cheap selective retrieval — holds. WorkBoard wins the *loop* because persistence is free, not because any single recall is smaller.
- **Honest — WorkBoard's heavier surface:** a per-turn protocol nudge (306 tok/turn). All-in (incl. the nudge) WorkBoard stays under mem0 up to ~11.7 turns/session at 3 recalls; trimmed to ~40 tok/turn that rises to ~89.2 turns. We publish the full crossover curve below, not a cherry-picked point.
- **Beats Letta head-to-head (REAL run):** Letta (MemGPT) re-sends ~3,444 tokens of memory machinery (blocks + memory-tool schemas + system prompt) **every turn**; WorkBoard carries 0 memory in context. Over a 100-session project WorkBoard runs the live loop with **81.0% fewer model tokens than Letta** (92.2% trimmed) — measured from Letta's own shipped prompts + tool schemas. See *Study 1b*.

## Method

- **In-repo & non-invasive.** Lives at `WorkBoard/Research/token_comparison/letta-comparison/`; reads a frozen `board_snapshot.json` + a read-only copy of `card.py`; writes only under this subfolder. A `lib/safety.py` guard refuses to write the live board or product source elsewhere.
- **Same tokenizer for all systems** (`tokencount.py`) — the fairness control that matters most.
- **WorkBoard = real, measured.** Recall via the actual `card.py` against the frozen snapshot (never the live board); bootstrap via the real harvest/bucketize path in a sandboxed `$HOME`.
- **mem0 = its own published numbers.** Retrieval ~1.8K tok/query and a single-pass ADD extraction call per session, from the Mem0 paper (arXiv:2504.19413) and mem0.ai/research-3. mem0 needs an OpenAI key + Qdrant to run; modeling it from its *own* marketed figures means we cannot be accused of mis-configuring or sandbagging it — the defaults FAVOR mem0 (flat 1.8K regardless of answer fan-out is its best case).
- **claude-mem = its own published numbers** (3-layer search economics, one compression call per session).
- **Correctness is real:** a WorkBoard answer counts only if every gold fact literally appears in a fetched card (`resolve_answer_cards` set-cover). Off-board facts are honest misses.

## Study 1 — Live memory loop (PRIMARY)

### (1) Memory-WRITE — model cost to persist each session's work

| System | LLM calls / session | model input tok / session | over 100 sessions |
|---|--:|--:|--:|
| **WorkBoard** (inline carding) | 0 | 0 | **0** |
| **mem0** (single-pass ADD) | 1 (+1 embed) | 5,462 | 546,200 |
| **claude-mem** (SessionEnd compress) | 1 | 5,462 | 546,200 |

WorkBoard's writeup is the main model's normal turn output, committed by the deterministic `card.py` CLI — **zero extra LLM calls**. mem0 and claude-mem each run one extraction/compression call per session over the ~5,462-token session (measured on the `medium` corpus). That's the tax that dominates the loop.

### (2) Memory I/O loop — 100 sessions × 3 recalls (HEADLINE)

Persist + recall combined (excludes WorkBoard's per-turn nudge, accounted separately in (4) — it is protocol overhead, not memory I/O):

| System | total model tokens | vs WorkBoard |
|---|--:|--:|
| **WorkBoard** | **719,700** | — |
| mem0 | 1,086,200 | WorkBoard **33.7%** fewer |
| claude-mem | 1,517,300 | WorkBoard **52.6%** fewer |

### (3) Per-recall, and the parallel *vs full-context* claim

| System | tok / recall | vs full-context (26,000) |
|---|--:|--:|
| **WorkBoard** | 2,399 | **−90.8%** |
| mem0 | 1,800 | −93.1% |
| claude-mem | 3,237 | — |

mem0's famous *“90% token savings”* is this column — vs stuffing the whole history. WorkBoard hits **−90.8%** on the same baseline, on par with mem0. Head-to-head per-recall, mem0's flat bundle is lighter (1,800 vs 2,399) — WorkBoard trades a slightly richer recall for free writes and structured lifecycle answers.

### (4) All-in crossover (honest — includes WorkBoard's per-turn nudge)

Total session tokens incl. the 306-tok/turn nudge (and a trimmed 40-tok variant) vs mem0's all-in (ADD + recalls, no per-turn injection):

| Turns | Recalls | WB all-in (full nudge) | WB all-in (trimmed) | mem0 all-in | WB(full) wins | WB(trim) wins |
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

At 3 recalls/session the full-nudge breakeven is ~11.7 turns; trimmed, ~89.2. The nudge is WorkBoard's one heavier surface — and it's the lever that makes writes free. Trim it and WorkBoard wins all-in across realistic sessions.

## Study 1b — WorkBoard vs Letta (REAL local measurement)

> Letta source: **real** (letta `0.16.8`, `letta artifacts (server-independent); model-independent`). Counted with the same `tiktoken-cl100k_base` tokenizer as every other system.

Letta (the MemGPT system) has a **structurally different** cost shape from mem0/claude-mem. Those pay a one-time extraction call *per session*. Letta pays **every turn**: its core memory blocks + memory-tool JSON schemas + MemGPT system prompt are re-sent to the model on every interaction (*“memory blocks are always visible … prepended to the prompt every interaction”* — docs.letta.com). Each memory write is an LLM tool call, and a Haiku compaction call fires when context fills. WorkBoard carries **no memory in context** (the board is never auto-loaded) — its only per-turn surface is the protocol nudge.

**Measured from Letta's own shipped artifacts** — its real system prompt (`memgpt_v2_chat`), the real JSON schemas of the base + memory tools a chat agent attaches (`memory_replace`, `memory_insert`, `archival_memory_*`, `conversation_search`), and the real `Memory.compile()` block rendering. This is server- and model-independent, so it reproduces anywhere `pip install letta` works.

### (1) Per-turn in-context memory overhead (the head-to-head)

| System | per-turn memory tokens | what it is |
|---|--:|---|
| **WorkBoard** | **306** (trimmable to 40) | protocol nudge; **0** memory carried in context |
| **Letta** (memory-attributable) | 2,318 | core blocks (130) + memory-tool schemas (2188) |
| **Letta** (full in-context) | 3,444 | + MemGPT system prompt (1061); blocks at full 5000-char capacity → 1975 |

Letta re-sends ~3.4K tokens of memory machinery **every turn**; WorkBoard sends a 306-token reminder and nothing else. That per-turn gap compounds over a session.

### (2) Live loop — 100 sessions × 50 turns × 3 recalls (HEADLINE)

| System | total model tokens | vs Letta |
|---|--:|--:|
| **WorkBoard** (full nudge) | **2,259,400** | **81.0% fewer** |
| **WorkBoard** (trimmed nudge) | 929,400 | 92.2% fewer |
| Letta (memory-attributable) | 11,909,200 | — |
| Letta (full in-context) | 17,539,200 | (WorkBoard 87.1% fewer) |

**WorkBoard runs the live memory loop with 81.0% fewer model tokens than Letta** (92.2% with a trimmed nudge) — the same order as claude-mem's *“95%”* and mem0's *“90%”* marketing, but head-to-head against Letta and measured from Letta's own code.

### (3) Write dimension + honest tradeoffs

- **Writes:** WorkBoard persists via a deterministic `card.py` call — **0 model tokens**. Letta persists via LLM memory tool calls (model output every time) and a Haiku compaction call when context fills.
- **Live cross-check (real run):** a local Letta server (Docker `letta/letta` + Ollama `llama3.2:3b`) replaying real session turns reported **~10,814 tokens/session** and emitted ~2.5 memory tool calls per session — i.e. its real per-turn cost *exceeds* the structural floor used above (the message buffer also grows), so the headline is conservative.
- **Honest — where Letta wins:** Letta's per-recall is a lean ~1,064-token archival fetch, **lighter than WorkBoard's content-rich cards** (2,399/recall). Letta also gives autonomous, cross-session, self-editing memory with **zero carding discipline** — it manages its own memory; WorkBoard needs the live-carding habit. WorkBoard wins the *loop* because it carries no memory in context and writes for free, not because any single recall is smaller.
- **Honest — WorkBoard's surface:** the 306-tok/turn nudge is WorkBoard's only per-turn cost; even un-trimmed it is ~7× lighter than Letta's per-turn memory payload, and it's the lever that makes writes free.

## Study 2 — Recall detail (3-way)

| Shape | n | WorkBoard | mem0 | vs mem0 | claude-mem | vs claude-mem |
|---|--:|--:|--:|--:|--:|--:|
| pinpoint | 6 | 2241 | 1800 | -24.5% | 2625 | **14.6%** |
| thematic | 7 | 2134 | 1800 | -18.6% | 2893 | **26.2%** |
| lifecycle | 6 | 2864 | 1800 | -59.1% | 4250 | **32.6%** |
| **all** | 19 | **2399** | 1800 | -33.3% | 3237 | **25.9%** |

Positive % = WorkBoard lighter. mem0's flat 1.8K bundle makes it the leanest per single recall (negative vs-mem0 numbers); WorkBoard is leanest vs claude-mem, especially on multi-card *lifecycle* questions where claude-mem fragments into many observations.

<details><summary>Per-query (20 queries)</summary>

| Query | Shape | WB total | mem0 | claude-mem |
|---|---|--:|--:|--:|
| P01 | pinpoint | 3397 | 1800 | 3000 |
| P02 | pinpoint | 2177 | 1800 | 3000 |
| P03 | pinpoint | 1318 | 1800 | 1500 |
| P04 | pinpoint | 2434 | 1800 | 3000 |
| P05 | pinpoint | 1740 | 1800 | 1500 |
| P06 | pinpoint | 855 *(board-miss)* | 1800 | 1500 |
| P07 | pinpoint | 2381 | 1800 | 3750 |
| T01 | thematic | 3788 | 1800 | 3750 |
| T02 | thematic | 2646 | 1800 | 3750 |
| T03 | thematic | 1734 | 1800 | 3750 |
| T04 | thematic | 1969 | 1800 | 2250 |
| T05 | thematic | 2106 | 1800 | 2250 |
| T06 | thematic | 1535 | 1800 | 2250 |
| T07 | thematic | 1163 | 1800 | 2250 |
| L01 | lifecycle | 4979 | 1800 | 7500 |
| L02 | lifecycle | 3936 | 1800 | 4500 |
| L03 | lifecycle | 2448 | 1800 | 3750 |
| L04 | lifecycle | 2264 | 1800 | 4500 |
| L05 | lifecycle | 1819 | 1800 | 3000 |
| L06 | lifecycle | 1739 | 1800 | 2250 |

</details>

## Study 3 — Bootstrap (secondary — cost to BUILD the memory)

De-emphasized by design (it's a one-time cost), but the same asymmetry holds: WorkBoard filters deterministically (hourly harvest + compact digests) before spending model tokens; mem0/claude-mem feed whole sessions to a model.

| Corpus | Sessions | WB calls | mem0 calls | WB input tok | mem0 input tok | Input reduction |
|---|--:|--:|--:|--:|--:|--:|
| tiny | 339 | 23 | 339 | 12,672 | 1,496,394 | **99.2%** |
| medium | 933 | 132 | 933 | 64,162 | 5,095,769 | **98.7%** |

## Where each system wins (honest)

**mem0 wins:**
- **Leanest single recall.** A flat ~1,800-token bundle beats WorkBoard's content-rich cards per query.
- **Zero-discipline, cross-project capture.** mem0 ingests automatically and spans projects; WorkBoard is project-scoped and needs the carding discipline.
- **Vague semantic recall** of things never carded — its vector store can surface them; WorkBoard's board simply doesn't hold off-board facts (e.g. board-miss ['P06']).

**WorkBoard wins:**
- **Free persistence** — no per-session extraction tax; this is what carries the live loop.
- **Structured, deterministic lifecycle recall** (origin → subtasks → writeup → links), reproducible and human-glanceable as a kanban.
- **Matches mem0's vs-full-context headline** (−90.8%).

They are complements: mem0 is conversational/semantic memory; WorkBoard is the structured project ledger. The honest one-liner: **mem0's 90% is vs a naive full-context baseline; WorkBoard matches that AND removes the per-write extraction tax mem0 still pays.**

## Reproduce

```bash
python3 build_fixtures.py        # freeze corpora from ~/.claude (once)
python3 run_recall.py            # 3-way recall
python3 run_live.py              # PRIMARY — live loop
python3 run_bootstrap.py         # secondary — build cost
python3 render_report.py         # regenerate this file
```

Standalone & non-invasive: recall reads a frozen `board_snapshot.json`; ingest runs in a throwaway `$HOME`; all product code is a read-only copy under `lib/product_scripts_ro/`. `board_snapshot.json` and `corpora/` are git-ignored (may contain private data); the code + `queries.json` + aggregate `results/` ship so anyone can re-derive every number.
