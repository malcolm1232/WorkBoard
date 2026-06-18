# mem0-comparison — WorkBoard vs mem0

A **self-contained, reproducible** efficiency study: **WorkBoard vs mem0**, focused
on the **live memory loop**. Everything needed to re-derive every number lives in
this one folder.

> **Scope:** this folder is **mem0 only** — no Letta, no claude-mem, no other work.
> Those comparisons live in their own sibling folders under
> `Research/token_comparison/`. Cards #730 / #734 / #749.

## Which document is which? (doc structure)

| Document | What it is | Read it when… |
|---|---|---|
| **`OVERVIEW.md`** | Plain-English walkthrough — how mem0 works, how WorkBoard works, method, scoreboard, where WB wins/loses | …you want to **understand** it fast (start here) |
| **`README.md`** (this) | Folder usage + this doc map + layout + reproduce steps | …you want to **run/navigate** the study |
| **`REPORT.md`** | The **headline report** — auto-generated: TL;DR + the 3 studies + key numbers | …you want **“the report”** |
| **`REPORT_DETAILED.md`** | The **exhaustive report** ("report full") — every table, full crossover grid, sources, limitations | …you want **everything** |
| **`CONTEXT.md`** | Full **step-by-step of what was done** + what it yields + how to resume without rerunning | …you (or a future session) need the **whole context** without re-running |
| `results/raw/*.json` | Machine-readable outputs every report is rendered from | …you want the **raw numbers** |

**Short version:** `OVERVIEW.md` = explainer · `REPORT.md` = the report ·
`REPORT_DETAILED.md` = the long report · `CONTEXT.md` = how-it-was-built + resume ·
`README.md` = folder usage.

## The headline

- **Live loop** (persist + recall, 100 sessions × 3 recalls): WorkBoard **33.7%
  fewer model tokens than mem0** — because mem0 spends an LLM extraction call every
  session and WorkBoard's carding is free.
- **Build memory:** WorkBoard **98.7% fewer** input tokens (and 7× fewer model calls).
- **Recall vs full-context:** WorkBoard saves **90.8%**, matching mem0's own "90%".
- **Honest:** mem0's flat ~1.8K per-query retrieval is **leaner** than WorkBoard's
  cards per single recall; WorkBoard wins the *loop*, not the *lookup*.

## Reproduce

```bash
python3 build_fixtures.py        # freeze corpora from ~/.claude (once)
python3 run_recall.py            # Study 2 (recall)
python3 run_live.py              # Study 1 (PRIMARY — live loop)
python3 run_bootstrap.py         # Study 3 (build cost)
python3 render_report.py         # → REPORT.md
python3 render_report_detailed.py# → REPORT_DETAILED.md
```
Deterministic — re-running yields identical numbers (no network, no model calls).

## Layout
```
mem0-comparison/
├── README.md  OVERVIEW.md  CONTEXT.md
├── REPORT.md  REPORT_DETAILED.md
├── tokencount.py            single shared tokenizer (the fairness control)
├── build_fixtures.py  corpus_stats.py  queries.json
├── peers/
│   ├── workboard_adapter.py   WorkBoard — MEASURED (real code, vendored)
│   └── mem0_adapter.py        mem0 — MODELED from its published numbers
├── run_recall.py  run_live.py  run_bootstrap.py
├── render_report.py  render_report_detailed.py
├── lib/
│   ├── safety.py             non-invasiveness guard + snapshot fingerprint
│   ├── card_ro.py            read-only copy of product card.py
│   └── product_scripts_ro/   read-only copy of product scripts/ (ingest path)
├── board_snapshot.json      frozen board copy   [git-ignored — may hold secrets]
├── corpora/{tiny,medium}/   frozen transcript fixtures   [git-ignored]
└── results/raw/             machine-readable outputs
```

## Safety & provenance

- **In-repo but non-invasive:** product code is vendored read-only under
  `lib/product_scripts_ro/`; `lib/safety.py` refuses to write the live board
  (`board/board.json`) or product source outside this folder. The live board is
  unchanged by running this study.
- **WorkBoard = measured** (its real code against a frozen board copy).
- **mem0 = modeled** from its own published per-op numbers (best case for mem0).
- `board_snapshot.json` and `corpora/` are git-ignored (may hold private data).
