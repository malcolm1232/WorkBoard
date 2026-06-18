# WorkBoard vs mem0 — Live Memory Efficiency Study (2026-06)

> **Auto-generated** by `render_report.py` from `results/raw/*.json`. Do not hand-edit the numbers — re-run the drivers and this renderer.
> Tokenizer: `tiktoken-cl100k_base` — the SAME tokenizer for both systems (the core fairness control). Card #730 / #734 / #749.
> Board snapshot: `7c49f1314c6b87d4` (1,155,340 B), a frozen COPY. Lives in-repo at `Research/token_comparison/mem0-comparison/` but is **non-invasive** — reads frozen copies, writes only here, never the live board (`lib/safety.py` enforces it). **This folder studies mem0 only.** Exhaustive companion: `REPORT_DETAILED.md`; plain-English explainer: `OVERVIEW.md`.

## TL;DR

- **Live loop (headline):** over a 100-session project at 3 recalls/session, WorkBoard runs the full memory loop (persist + recall) with **33.7% fewer model tokens than mem0** (719,700 vs 1,086,200). The reason is structural: **mem0 spends an LLM extraction call on *every* session** (~5,462 input tok), while WorkBoard's carding is inline in the agent's normal turn — **0 dedicated LLM calls**.
- **Matches mem0's own headline:** mem0 markets *“90% fewer tokens vs full-context.”* On the same 26,000-token baseline, WorkBoard recall is **90.8% lighter** (mem0: 93.1%). WorkBoard can make the *same* vs-full-context claim — and additionally beats mem0 head-to-head on the loop.
- **Honest — mem0 wins the single recall.** mem0's flat ~1,800-token bundle is **leaner than WorkBoard's content-rich cards** (2,399 tok/recall). WorkBoard wins the *loop* because persistence is free, not because any single recall is smaller.
- **Honest — WorkBoard's heavier surface:** a per-turn protocol nudge (306 tok/turn). All-in (incl. the nudge) WorkBoard stays under mem0 up to ~11.7 turns/session at 3 recalls; trimmed to ~40 tok/turn that rises to ~89.2 turns. Full crossover curve below.

## Method

- **In-repo & non-invasive.** Reads a frozen `board_snapshot.json` + a read-only copy of `card.py`; writes only under this folder.
- **Same tokenizer for both systems** (`tokencount.py`).
- **WorkBoard = real, measured.** Recall via the actual `card.py` against the frozen snapshot; bootstrap via the real harvest/bucketize path in a sandboxed `$HOME`.
- **mem0 = its own published numbers.** Retrieval ~1.8K tok/query and a single-pass ADD extraction call per session, from the Mem0 paper (arXiv:2504.19413) and mem0.ai/research-3. Defaults FAVOR mem0 (flat 1.8K regardless of fan-out is its best case) — so any WorkBoard margin is a floor.
- **Correctness is real:** a WorkBoard answer counts only if every gold fact literally appears in a fetched card (`resolve_answer_cards`). Off-board facts are honest misses (mem0 wins those).

## Study 1 — Live memory loop (PRIMARY)

### (1) Memory-WRITE — model cost to persist each session's work

| System | LLM calls / session | model input tok / session | over 100 sessions |
|---|--:|--:|--:|
| **WorkBoard** (inline carding) | 0 | 0 | **0** |
| **mem0** (single-pass ADD) | 1 (+1 embed) | 5,462 | 546,200 |

WorkBoard's writeup is the main model's normal turn output, committed by the deterministic `card.py` CLI — **zero extra LLM calls**. mem0 runs one ADD extraction call per session over the ~5,462-token session (measured on the `medium` corpus). That tax dominates the loop.

### (2) Memory I/O loop — 100 sessions × 3 recalls (HEADLINE)

Persist + recall combined (excludes WorkBoard's per-turn nudge — that's protocol overhead, accounted separately in (4)):

| System | total model tokens | vs WorkBoard |
|---|--:|--:|
| **WorkBoard** | **719,700** | — |
| mem0 | 1,086,200 | WorkBoard **33.7%** fewer |

### (3) Per-recall, and the parallel *vs full-context* claim

| System | tok / recall | savings vs full-context (26,000) |
|---|--:|--:|
| **WorkBoard** | 2,399 | **90.8% fewer** |
| mem0 | 1,800 | 93.1% fewer |

(“fewer” = reduction vs the naive baseline — same direction as mem0's marketed “90% fewer”.)

mem0's famous *“90% token savings”* is this column — vs stuffing the whole history. WorkBoard saves **90.8%** on the same baseline. Head-to-head per single recall, mem0's flat bundle is lighter (1,800 vs 2,399) — WorkBoard trades a slightly richer recall for free writes and structured lifecycle answers.

### (4) All-in crossover (honest — includes WorkBoard's per-turn nudge)

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

At 3 recalls/session the full-nudge breakeven is ~11.7 turns; trimmed, ~89.2. Trim the nudge and WorkBoard wins all-in across realistic sessions.

## Study 2 — Recall detail (WorkBoard vs mem0)

| Shape | n | WorkBoard | mem0 | WB vs mem0 |
|---|--:|--:|--:|--:|
| pinpoint | 6 | 2241 | 1800 | -24.5% |
| thematic | 7 | 2134 | 1800 | -18.6% |
| lifecycle | 6 | 2864 | 1800 | -59.1% |
| **all** | 19 | **2399** | 1800 | -33.3% |

Positive % = WorkBoard lighter. mem0's flat 1.8K bundle makes it the leanest per single recall (negative numbers) — WorkBoard's edge is the *loop*, not the *lookup*.

## Study 3 — Bootstrap (secondary — cost to BUILD the memory)

| Corpus | Sessions | WB calls | mem0 calls | WB input tok | mem0 input tok | Input reduction |
|---|--:|--:|--:|--:|--:|--:|
| tiny | 339 | 23 | 339 | 12,672 | 1,496,394 | **99.2%** |
| medium | 933 | 132 | 933 | 64,162 | 5,095,769 | **98.7%** |

WorkBoard buckets work hourly and feeds compact digests (a deterministic, no-model pre-pass); mem0 feeds whole sessions to a model.

## Where each system wins (honest)

**mem0 wins:** the leanest single recall (flat ~1,800 tok); zero-discipline automatic cross-project capture; vague semantic recall of off-board facts (board-miss ['P06']).

**WorkBoard wins:** free persistence (no per-session extraction tax — this carries the loop); structured, deterministic lifecycle recall; matches mem0's vs-full-context headline (90.8% fewer).

Complements, not substitutes: mem0 = automatic cross-project semantic memory; WorkBoard = the structured, free-to-maintain project ledger. mem0's “90%” is vs a naive baseline — WorkBoard matches that AND removes the per-write extraction tax mem0 still pays.

## Reproduce

```bash
python3 build_fixtures.py        # freeze corpora from ~/.claude (once)
python3 run_recall.py            # Study 2
python3 run_live.py              # Study 1 (PRIMARY)
python3 run_bootstrap.py         # Study 3
python3 render_report.py         # regenerate this file
python3 render_report_detailed.py# the exhaustive companion
```

Deterministic — re-running yields identical numbers (no network, no model calls). `board_snapshot.json` and `corpora/` are git-ignored (private).
