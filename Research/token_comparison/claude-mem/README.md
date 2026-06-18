# claude-mem-comparison

A **self-contained, reproducible** efficiency study: **WorkBoard vs claude-mem**
(with mem0 cited). Everything needed to re-derive every number lives in this one
folder — it does **not** depend on the WorkBoard product being present.

Card #730 / #736 / #737 / #739. Tokenizer: `tiktoken cl100k` (same for both sides).

---

## Which document is which? (doc map)

This folder has several docs at different zoom levels. Pick by what you want:

| Document | What it is | Read it when… |
|---|---|---|
| **`OVERVIEW.md`** | Plain-English walkthrough — how claude-mem works, how WorkBoard works, method, scoreboard, where WB wins/loses | …you want to **understand** the comparison fast (start here) |
| `README.md` (this) | Folder usage + this doc map + reproduce steps | …you want to **run/navigate** the study |
| **`REPORT.md`** | The **headline report** — concise: TL;DR + the 3 studies + key numbers | …you want **“the report”** |
| **`REPORT_FULL.md`** | The **exhaustive report** — every table + real run + caveats | …you want **everything** |
| `REPORT_BOOTSTRAP.md` | Study A only (cost to **build** the memory), detailed | …you care about ingest/bootstrap specifically |
| `REPORT_LIVE.md` | Study C only (cost to **run** with memory on), detailed | …you care about live/steady-state specifically |
| `PROCESS_LOG.md` | Step-by-step of everything done — audit trail | …you want to know **how it was built** / resume without rerunning |
| `REPRODUCIBILITY.md` | Provenance: what's measured vs modeled vs cited, per number | …you want to **verify/reproduce** |
| `REAL_RUN_FINDINGS.md` | What the real sandboxed claude-mem run established | …you want the **real-run validation** |
| `run_claude_mem_tiny.md` | Steps to run real claude-mem yourself | …you want to **re-run the peer** |

**Short version:** `OVERVIEW.md` = friendly explainer · `REPORT.md` = the report ·
`REPORT_FULL.md` = the long report · `README.md` = folder usage. (Study B = recall;
its numbers live in `REPORT.md`/`REPORT_FULL.md`, not a standalone file.)

> **Scope note:** this folder is the **claude-mem** deep-dive only. The *mem0* and
> *Letta* comparisons live in the sibling folder `../letta-comparison/` (a combined
> live-loop study of WorkBoard vs mem0 + claude-mem + Letta).

## The headline

- **Bootstrap (build memory):** WorkBoard uses **98.6–99.2% fewer model-input
  tokens** and 5–15× fewer model calls than claude-mem on the same corpus.
- **Recall (use memory):** WorkBoard loads **25.9% fewer tokens** to answer
  (33% on lifecycle queries); wins 16/19. claude-mem wins tight pinpoints + 1
  off-board fact.
- **Live (persist work):** WorkBoard adds **0 model calls/session** (inline
  carding) vs claude-mem's 1 full-tier compression call (~5,462 tok) → **0 vs
  546,200 tokens over 100 sessions**.
- All conservative (settings favor claude-mem). Honest tradeoffs documented.

## Reproduce (one folder, no product needed)

```bash
cd ~/Desktop/WorkBoard/Research/token_comparison/claude-mem
python3 run_bootstrap.py      # Study A   -> results/raw/bootstrap.json
python3 run_recall.py         # Study B   -> results/raw/recall.json
python3 replay_session.py     # Study C   -> results/raw/live.json
python3 render_report.py      # -> REPORT.md
python3 report_bootstrap.py   # -> REPORT_BOOTSTRAP.md
python3 report_live.py        # -> REPORT_LIVE.md
python3 report_full.py        # -> REPORT_FULL.md
```
Deterministic — re-running yields identical numbers (no network, no model calls).

## What's in here

```
claude-mem/                         (Research/token_comparison/claude-mem)
├── README.md  OVERVIEW.md  PROCESS_LOG.md  REPRODUCIBILITY.md  REAL_RUN_FINDINGS.md
├── REPORT_FULL.md  REPORT.md  REPORT_BOOTSTRAP.md  REPORT_LIVE.md
├── run_claude_mem_tiny.md          # optional real claude-mem validation steps
├── tokencount.py                   # shared tokenizer (the fairness control)
├── corpus_stats.py  build_fixtures.py  queries.json
├── peers/
│   ├── workboard_adapter.py        # WorkBoard — MEASURED (real code, vendored)
│   └── claude_mem_adapter.py       # claude-mem — MODELED from its published #s
├── run_bootstrap.py run_recall.py replay_session.py
├── render_report.py report_bootstrap.py report_live.py report_full.py
├── lib/product_scripts_ro/         # read-only vendored copy of WorkBoard/scripts/*
├── board_snapshot.json             # frozen board copy (local; never the live board)
├── corpora/{tiny,medium,large}/    # frozen transcript fixtures (local)
├── reference/                      # TOKEN_BUDGET.md + COMPARISON.md snapshots (cited)
└── results/raw/                    # machine-readable outputs
```

## Safety & provenance (the short version)

- **Self-contained:** product scripts are vendored read-only under
  `lib/product_scripts_ro/`. Nothing here reads or writes the live product.
- **WorkBoard = measured** (its real code run against a frozen board copy).
- **claude-mem = modeled** from its own published per-layer numbers; a real
  sandboxed run validated the *structure* (see `REAL_RUN_FINDINGS.md`).
- `board_snapshot.json` and `corpora/` are local-only (may hold private data).

See `REPRODUCIBILITY.md` for the full per-number provenance table.
