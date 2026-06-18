# WorkBoard vs mem0 — DETAILED Efficiency Study

> **Auto-generated** by `render_report_detailed.py` from `results/raw/*.json`. Every number is derived; do not hand-edit. Companion to the shorter `REPORT.md`; plain-English explainer in `OVERVIEW.md`. Cards #730 / #734 / #749.

## 0. Provenance & fairness fingerprint

| Field | Value |
|---|---|
| Tokenizer (both systems) | `tiktoken-cl100k_base` |
| Board snapshot | `7c49f1314c6b87d4` (1,155,340 B) |
| WorkBoard | REAL — `card.py` against frozen snapshot |
| mem0 | MODELED from its own published numbers |
| Location | `Research/token_comparison/mem0-comparison/` (in-repo, non-invasive) |

The single most important fairness control: **one tokenizer (`tiktoken-cl100k_base`) counts every token for both systems.** It is documented to run ~10–15% *under* Claude's true tokenizer, so absolute counts are conservative and the *ratios* we report are tokenizer-invariant.

## 1. Executive summary

| Metric | WorkBoard | mem0 | Result |
|---|--:|--:|---|
| Build memory (input tok) | 64,162 | 5,095,769 | **WB 98.7% fewer** |
| Persist / session | 0 | 1 LLM call (~5,462 tok) | **WB free** |
| Live loop / 100 sessions | 719,700 | 1,086,200 | **WB 33.7% fewer** |
| Per single recall | 2,399 | 1,800 | **mem0 leaner** |
| Recall savings vs full-context | 90.8% fewer | 93.1% fewer | **WB ≈ matches mem0's “90%”** |

**One-sentence finding:** mem0's marketing number (“90% fewer tokens”) is vs a naive *full-context* baseline, not a peer. Head-to-head on real history, WorkBoard runs the live loop with **33.7% fewer model tokens** — because its writes are free, while mem0 pays an LLM extraction call every session. WorkBoard does **not** win every single recall (mem0's flat bundle is leaner); it wins the loop.

## 2. Definitions

- **Live loop** — steady-state cost of working with memory ON: WRITE (persist) + RECALL (use), projected over a project lifetime.
- **Memory-WRITE** — tokens/calls to store what happened. WorkBoard: 0 dedicated calls (the writeup is the agent's normal turn output, committed by `card.py`). mem0: one single-pass ADD extraction LLM call per session.
- **Recall** — tokens injected to answer one query. WorkBoard: real two-layer `card.py` retrieval. mem0: flat ~1.8K top-k bundle.
- **Full-context baseline** — the naive alternative of pasting the whole history each query (~26,000 tok). mem0's “90%” is vs this, NOT vs a peer.
- **All-in / crossover** — WorkBoard's one recurring tax is a per-turn protocol nudge (306 tok, trimmable to ~40). The crossover shows at what session length that tax erodes the loop advantage.

## 3. Method & fairness controls

1. **Same tokenizer** for both (`tokencount.py`).
2. **Same frozen corpus**, byte-fingerprinted (§4); excludes the 2026-06-11→15 inactivity gap.
3. **mem0 measured by its own evidence** — published retrieval (~1.8K/query) + single-pass ADD (1 LLM call/session). Defaults FAVOR mem0.
4. **Gold answers pre-written** in `queries.json` before querying.
5. **Correctness is real** — a WorkBoard answer counts only if every gold fact literally appears in a fetched card. Off-board facts are honest mem0 wins.
6. **Non-invasive & deterministic** — reads frozen copies, writes only here, re-runs byte-identical.

## 4. The corpus (frozen fixtures)

| Corpus | Window | Files | Bytes | Fingerprint | Sessions | Turns | Transcript tok |
|---|---|--:|--:|---|--:|--:|--:|
| tiny | 2026-06-16→2026-06-17 | 339 | 37,052,825 | `bd952ca8bc283a8e` | 339 | 2,302 | 1,496,394 |
| medium | 2026-05-28→2026-06-10 | 933 | 186,478,743 | `94c784a7c731351b` | 933 | 11,693 | 5,095,769 |

The live-loop numbers use the **medium** corpus (avg session = 5,462 transcript tokens — what mem0's per-session ADD must read).

## 5. Study 1 — Live loop

### 5.1 Memory-WRITE per session
| System | LLM calls/session | input tok/session | × 100 sessions |
|---|--:|--:|--:|
| WorkBoard (inline carding) | 0 | 0 | **0** |
| mem0 (single-pass ADD) | 1 (+1 embed) | 5,462 | 546,200 |

### 5.2 Memory I/O loop — 100 sessions × 3 recalls (HEADLINE)
| System | total model tokens | vs WorkBoard |
|---|--:|--:|
| **WorkBoard** | **719,700** | — |
| mem0 | 1,086,200 | WorkBoard **33.7%** fewer |

### 5.3 Per-recall + the parallel vs-full-context claim
| System | tok/recall | savings vs full-context (26,000) |
|---|--:|--:|
| WorkBoard | 2,399 | **90.8% fewer** |
| mem0 | 1,800 | 93.1% fewer |

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

## 6. Study 2 — Recall (full 20-query detail)

| Shape | n | WorkBoard | mem0 | WB vs mem0 |
|---|--:|--:|--:|--:|
| pinpoint | 6 | 2241 | 1800 | -24.5% |
| thematic | 7 | 2134 | 1800 | -18.6% |
| lifecycle | 6 | 2864 | 1800 | -59.1% |
| **all** | 19 | **2399** | 1800 | -33.3% |

Per-query (WorkBoard index/detail split; mem0 = flat bundle):

| Query | Shape | WB idx | WB detail | WB total | found | mem0 | answer cards |
|---|---|--:|--:|--:|:--:|--:|---|
| P01 | pinpoint | 861 | 2536 | 3397 | ✓ | 1800 | #627,#645,#646 |
| P02 | pinpoint | 835 | 1342 | 2177 | ✓ | 1800 | #608,#609,#624 |
| P03 | pinpoint | 820 | 498 | 1318 | ✓ | 1800 | #215 |
| P04 | pinpoint | 827 | 1607 | 2434 | ✓ | 1800 | #74,#75,#454 |
| P05 | pinpoint | 357 | 1383 | 1740 | ✓ | 1800 | #598 |
| P06 | pinpoint | 855 | 0 | 855 | miss | 1800 |  |
| P07 | pinpoint | 762 | 1619 | 2381 | ✓ | 1800 | #634,#635,#636,#637 |
| T01 | thematic | 816 | 2972 | 3788 | ✓ | 1800 | #570,#627,#640,#645 |
| T02 | thematic | 796 | 1850 | 2646 | ✓ | 1800 | #73,#74,#75,#454 |
| T03 | thematic | 840 | 894 | 1734 | ✓ | 1800 | #494,#502,#503,#535 |
| T04 | thematic | 857 | 1112 | 1969 | ✓ | 1800 | #443,#633 |
| T05 | thematic | 584 | 1522 | 2106 | ✓ | 1800 | #563,#673 |
| T06 | thematic | 526 | 1009 | 1535 | ✓ | 1800 | #299,#576 |
| T07 | thematic | 496 | 667 | 1163 | ✓ | 1800 | #78,#503 |
| L01 | lifecycle | 828 | 4151 | 4979 | ✓ | 1800 | #627,#639,#640,#641,#642,#643,#644,#645,#646 |
| L02 | lifecycle | 808 | 3128 | 3936 | ✓ | 1800 | #608,#609,#610,#611,#624 |
| L03 | lifecycle | 829 | 1619 | 2448 | ✓ | 1800 | #634,#635,#636,#637 |
| L04 | lifecycle | 868 | 1396 | 2264 | ✓ | 1800 | #570,#572,#573,#576,#577 |
| L05 | lifecycle | 732 | 1087 | 1819 | ✓ | 1800 | #103,#107,#384 |
| L06 | lifecycle | 806 | 933 | 1739 | ✓ | 1800 | #626,#668 |

Board-misses (facts not on the board → honest mem0 wins): ['P06']. WorkBoard answered 19/20 queries.

## 7. Study 3 — Bootstrap (build cost, secondary)

| Corpus | Sessions | WB calls | mem0 calls | WB input tok | mem0 input tok | Reduction |
|---|--:|--:|--:|--:|--:|--:|
| tiny | 339 | 23 | 339 | 12,672 | 1,496,394 | **99.2%** |
| medium | 933 | 132 | 933 | 64,162 | 5,095,769 | **98.7%** |

WorkBoard buckets hourly and feeds compact digests (deterministic, no-model pre-pass); mem0 feeds whole sessions to a model → 1–2 orders of magnitude more input tokens.

## 8. Where each system wins (honest)

**WorkBoard wins:** free persistence (no per-session extraction tax — carries the loop); structured, deterministic, reproducible lifecycle recall; human-glanceable kanban; matches mem0's vs-full-context headline (90.8% fewer).

**mem0 wins:** leanest single recall (flat ~1,800 tok); zero-discipline automatic cross-project capture; vague semantic recall of things never carded (board-miss ['P06']).

## 9. Limitations & threats to validity

- **mem0 is modeled, not run.** It needs an OpenAI key + Qdrant; we use its OWN published per-op numbers (best case), so error favors mem0.
- **mem0's flat 1.8K recall** is held constant across query fan-out — generous to mem0 on multi-fact queries. It still wins per-recall here; we did not tilt this our way.
- **The per-turn nudge is treated as protocol overhead** (excluded from the I/O-loop headline, included in the crossover §5.4). A skeptic who counts it as memory cost should note WorkBoard then needs the trimmed nudge to win long sessions.
- **Single-user corpus** — ratios should generalize; absolute counts are corpus-specific.
- **tiktoken ≈ 10–15% under Claude's true tokenizer** — applied equally, so ratios are unaffected.

## 10. mem0 constants & sources (appendix)

`peers/mem0_adapter.py` — sources: arXiv:2504.19413 + mem0.ai/research-3:
- `recall_tokens_per_query` = 1800
- `recall_beam_long_context` = 6719
- `add_llm_calls_per_session` = 1
- `embed_calls_per_session` = 1
- `full_context_tokens_per_query` = 26000

## 11. Exact reproduction

```bash
cd Research/token_comparison/mem0-comparison
python3 build_fixtures.py        # freeze corpora from ~/.claude (once; reads only)
python3 run_recall.py            # Study 2
python3 run_live.py              # Study 1 (PRIMARY)
python3 run_bootstrap.py         # Study 3
python3 render_report.py         # short REPORT.md
python3 render_report_detailed.py# this file
```

All inputs frozen: `board_snapshot.json` (exact board), `corpora/*/manifest.json` (fingerprinted transcripts), `results/raw/*.json` (every number). Re-runs are byte-identical. `board_snapshot.json` + `corpora/` are git-ignored (private); the code + `queries.json` ship so anyone can re-derive everything.
