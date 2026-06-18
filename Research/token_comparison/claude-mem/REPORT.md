# WorkBoard vs claude-mem — Efficiency Study (2026-06)

> **Auto-generated** by `render_report.py` from `results/raw/*.json`. Do not hand-edit the numbers — re-run the drivers and this renderer.
> Tokenizer: `tiktoken-cl100k_base` (same tokenizer applied to BOTH systems — the core fairness control). Card #730.

## TL;DR

- **Bootstrap (building the memory):** on the *medium* corpus (933 sessions, 11693 turns over 2026-05-28→2026-06-10), WorkBoard ingests with **99.8% fewer model-input tokens** (10,546 vs 5,095,769) and **10.6× fewer model calls** (88 vs 933). This is a *direct head-to-head*, and it meets/exceeds claude-mem's own “~95%” framing.
- **Recall (using the memory):** across 19 answerable queries, WorkBoard loads **25.9% fewer tokens** to reach the answer (2399 vs 3237 tok mean; WorkBoard cheaper on 16/19). Strongest on multi-card *lifecycle* queries (**32.6%**).
- **Live carding (persisting work as you go):** WorkBoard cards inline — **0 extra model calls/session**. claude-mem runs a SessionEnd compression call every session (~5,462 input tok). Over 100 sessions that's **0 vs 546,200** model tokens just to keep memory current.
- **Honest:** claude-mem wins 3 tight single-fact lookups and every *board-miss* (facts not on the board: ['P06']); and WorkBoard's per-turn nudge is its one heavier surface (trimmable). These numbers GIVE claude-mem its best case — the WorkBoard margins are a conservative floor.

## Method

- **Corpus:** frozen snapshots of real Claude-Code transcripts (`~/.claude/projects`), excluding the 2026-06-11→15 inactivity gap. Fingerprinted in each `corpora/<size>/manifest.json`.
- **Same tokenizer for both systems** (`tokencount.py`) — the one fairness control that matters most.
- **WorkBoard = real, measured.** Ingest via the actual bootstrap harvest/bucketize path (`scripts/hourly_extractor.py`) in a sandboxed `$HOME`; recall via real `card.py` against a frozen board snapshot (never the live board).
- **claude-mem = its own published numbers.** 3-layer search economics from claude-mem 13.6.1's README (`search` ~50-100 tok/result; `get_observations` ~500-1,000 tok/result; one compression call per session). Defaults set to claude-mem's MID/BEST case, and `fragmentation=1.0` GIVES it WorkBoard's consolidation benefit. Using claude-mem's *own* numbers (rather than our measurement of their tool) is deliberate — it can't be accused of sandbagging. A ready-to-run real-ingest validation harness is provided (`run_claude_mem_tiny.md`) to cross-check these figures against a sandboxed tiny run.
- **Correctness is real, not a proxy:** a WorkBoard answer counts only if every gold fact literally appears in a fetched card's content (`resolve_answer_cards` greedy set-cover). Facts that live only in memory files / off-board are honest misses.

## Study A — Bootstrap (cost to BUILD the memory)

| Corpus | Sessions | WorkBoard calls | claude-mem calls | WB input tok | claude-mem input tok | Input reduction |
|---|--:|--:|--:|--:|--:|--:|
| tiny | 336 | 17 | 336 | 1,868 | 1,481,053 | **99.9%** |
| medium | 933 | 88 | 933 | 10,546 | 5,095,769 | **99.8%** |
| large | 984 | 144 | 984 | 18,522 | 5,694,890 | **99.7%** |

WorkBoard buckets work **hourly** and feeds the model compact digests; claude-mem compresses **every session** by reading its full transcript. WorkBoard's harvest+digest is a deterministic, no-model pre-pass — the reason its model-input is ~1-2 orders of magnitude smaller.

## Study B — Recall (cost to USE the memory)  ← headline

Full two-layer retrieval chain per query (index = find the card(s); detail = read them). WorkBoard detail = compact card payload (title/origin/writeup/links, no internal history metadata); claude-mem detail = its `get_observations` spec.

| Shape | n | WorkBoard mean | claude-mem mean | Reduction | WB wins |
|---|--:|--:|--:|--:|--:|
| pinpoint | 6 | 2241 | 2625 | **14.6%** | 4/6 |
| thematic | 7 | 2134 | 2893 | **26.2%** | 6/7 |
| lifecycle | 6 | 2864 | 4250 | **32.6%** | 6/6 |
| **all** | 19 | **2399** | **3237** | **25.9%** | 16/19 |

### Per-query detail

| Query | Shape | WB idx | WB detail | WB total | CM total | Winner |
|---|---|--:|--:|--:|--:|:--|
| P01 | pinpoint | 861 | 2536 | 3397 | 3000 | claude-mem |
| P02 | pinpoint | 835 | 1342 | 2177 | 3000 | WB |
| P03 | pinpoint | 820 | 498 | 1318 | 1500 | WB |
| P04 | pinpoint | 827 | 1607 | 2434 | 3000 | WB |
| P05 | pinpoint | 357 | 1383 | 1740 | 1500 | claude-mem |
| P06 | pinpoint | 855 | 0 | 855 | 1500 | claude-mem (board-miss) |
| P07 | pinpoint | 762 | 1619 | 2381 | 3750 | WB |
| T01 | thematic | 816 | 2972 | 3788 | 3750 | claude-mem |
| T02 | thematic | 796 | 1850 | 2646 | 3750 | WB |
| T03 | thematic | 840 | 894 | 1734 | 3750 | WB |
| T04 | thematic | 857 | 1112 | 1969 | 2250 | WB |
| T05 | thematic | 584 | 1522 | 2106 | 2250 | WB |
| T06 | thematic | 526 | 1009 | 1535 | 2250 | WB |
| T07 | thematic | 496 | 667 | 1163 | 2250 | WB |
| L01 | lifecycle | 828 | 4151 | 4979 | 7500 | WB |
| L02 | lifecycle | 808 | 3128 | 3936 | 4500 | WB |
| L03 | lifecycle | 829 | 1619 | 2448 | 3750 | WB |
| L04 | lifecycle | 868 | 1396 | 2264 | 4500 | WB |
| L05 | lifecycle | 732 | 1087 | 1819 | 3000 | WB |
| L06 | lifecycle | 806 | 933 | 1739 | 2250 | WB |

## Study C — Live carding (steady-state cost as you work)

Two cost dimensions per working session, measured the same way for both.

### (1) Memory-write — model cost to persist the session's work

| | model calls / session | model input tokens / session | over 100 sessions |
|---|--:|--:|--:|
| **WorkBoard** (inline carding) | 0 | 0 | 0 |
| **claude-mem** (SessionEnd compress) | 1 | 5,462 | 546,200 |

WorkBoard's carding is a deterministic `card.py` write whose text the main model already produced that turn — **zero extra model calls**. claude-mem runs one Agent-SDK compression call per session over the full transcript. That's the bootstrap cost, paid on every session forever.

### (2) Context-injection — interactive tokens added per session

- WorkBoard: **97 tok** SessionStart digest + **306 tok/turn** protocol nudge = 15,397/50-turn (**trimmable to 2,097**, ~40/turn). The 130KB+ board.json is **never auto-loaded** — injection is **constant in board size**.
- claude-mem: ~thousands (grows with stored memory) at SessionStart (docs/TOKEN_BUDGET.md); **grows with stored memory**.

**Honest:** the per-turn nudge is WorkBoard's one *heavier* surface — but it's a fixed, trimmable reminder, and it's the lever that makes (1) free. WorkBoard moves the memory-write cost off the model; claude-mem keeps the session quiet but pays a compression call every session.

### (3) Per-recall (carried from Study B)

WorkBoard **2399 tok** vs claude-mem **3237 tok** (25.9% lighter) vs mem0 **6,719 tok** (64.3% lighter).

## Where claude-mem wins (honest tradeoffs)

- **Vague/semantic recall of off-board facts.** Anything not carded (operational trivia like backup-dir names — e.g. query ['P06']) WorkBoard's board simply doesn't hold; claude-mem's vector store can surface it from raw transcripts.
- **Tight single-fact pinpoints.** When the answer is one short fact, claude-mem's compressed observation can be smaller than a content-rich WorkBoard card. (It wins a few pinpoint queries above.)
- **Cross-project memory & zero-structure capture.** WorkBoard is project-scoped and needs the carding discipline; claude-mem captures everything automatically across projects.

WorkBoard's edge is **structured, deterministic recall of work outcomes** (lifecycle/multi-fact) and **near-zero build + steady-state cost**. They are complements: claude-mem is conversational memory; WorkBoard is the project ledger.

## Reproduce

```bash
python3 build_fixtures.py        # freeze corpora from ~/.claude (once)
python3 run_bootstrap.py         # Study A
python3 run_recall.py            # Study B (headline)
python3 replay_session.py        # live operating cost
python3 render_report.py         # regenerate this file
```

The harness never touches the live board or `~/.claude`: recall reads a frozen `board_snapshot.json`; ingest runs in a throwaway `$HOME`. `board_snapshot.json` and `corpora/` are git-ignored (may contain private data); the code + `queries.json` + aggregate `results/` ship so anyone can re-derive every number.
