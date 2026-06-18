# token_comparison — WorkBoard vs AI-memory systems

Head-to-head **token-efficiency** studies comparing **WorkBoard** against the
shipping memory/knowledge systems: **mem0**, **claude-mem**, **Letta (MemGPT)**, and
**graphify**. Same corpus, same tokenizer (`tiktoken cl100k`) for every system — the
core fairness control. Peers are measured/modeled from their **own** published
numbers or shipped code, with settings that **favor the peer**, so WorkBoard's
margins are conservative floors.

### 👉 Start with **[`MASTER_SUMMARY.md`](MASTER_SUMMARY.md)** — the cross-peer roll-up (all results, two tables).

Each peer also has its **own self-contained, reproducible folder** (run it alone, or
zip it and hand it over). Nothing here touches the live product: each study reads a
frozen `board_snapshot.json` and writes only inside its own folder (`lib/safety.py`
enforces it).

## Peers

| Study folder | What it compares | Headline (vs WorkBoard) |
|---|---|---|
| **[mem0-comparison/](mem0-comparison/)** | WorkBoard vs **mem0** (standalone) | Live loop **33.7% fewer** tokens · build **98.7% fewer** · persists **free**. Honest: mem0 leaner per single recall (1,800 vs 2,399). |
| **[claude-mem/](claude-mem/)** | WorkBoard vs **claude-mem** (deep-dive: bootstrap + recall + real run) | Build **~99% fewer** · recall **25.9% lighter** (wins 16/19) · live **0 vs 546K** tok/100 sessions. |
| **[letta-comparison/](letta-comparison/)** | WorkBoard vs **Letta** (+ combined mem0/claude-mem/Letta live-loop harness) | Letta live loop **81.0% fewer** (92.2% trimmed) — Letta's tax is per-*turn* (blocks + tool schemas re-sent every turn). |
| **[graphify-comparison/](graphify-comparison/)** | WorkBoard vs **graphify** (code knowledge-graph; real `graphifyy 0.8.41` install) | **No 95%-style win — and that's the finding.** Different *shape* (work outcomes vs code structure); both write free; WorkBoard SKILL.md **28.5% lighter**; graphify lighter per-prompt. |

> mem0 appears in two places by design: **`mem0-comparison/`** is the dedicated
> mem0-only study; **`letta-comparison/`** also runs mem0 as one baseline inside the
> combined live-loop harness.

## The one-liners

- WorkBoard **builds** memory with **~98–99% fewer tokens** than mem0/claude-mem.
- WorkBoard **persists** work for **free** (0 model calls/session vs an LLM call every
  session for mem0/claude-mem; per-turn for Letta).
- Over a project's life, WorkBoard runs the memory loop **34–81% cheaper**.
- **Honest:** mem0 and Letta are *leaner per single recall* — WorkBoard wins the
  **loop**, not the **lookup**. graphify is a different-domain tool (complement).

## Layout

```
Research/token_comparison/         (tracked; private data git-ignored — see .gitignore)
├── README.md                      (this index)
├── MASTER_SUMMARY.md              ← cross-peer roll-up — start here
├── mem0-comparison/               WorkBoard vs mem0 (standalone, mem0 only)
│   └── OVERVIEW.md  REPORT.md  REPORT_DETAILED.md  CONTEXT.md  run_*.py  peers/
├── claude-mem/                    WorkBoard vs claude-mem (deep-dive + real run)
│   └── OVERVIEW.md  REPORT.md  REPORT_FULL.md  PROCESS_LOG.md  REPRODUCIBILITY.md …
├── letta-comparison/              WorkBoard vs Letta (+ combined mem0/claude-mem/Letta)
│   └── OVERVIEW.md  REPORT.md  REPORT_DETAILED.md  CONTEXT.md  letta_*.py  peers/
└── graphify-comparison/           WorkBoard vs graphify (real sandboxed install)
    └── REPORT.md  REPORT_DETAILED.md  CONTEXT.md  run_live_graphify.py …

Per-folder (git-ignored, local-only): board_snapshot.json · corpora/ · results/raw/
· lib/product_scripts_ro/ (read-only vendored product code).
```

Each folder also carries `OVERVIEW.md` (plain-English explainer), `REPORT.md` (the
report), a detailed/full report, and `CONTEXT.md`/`PROCESS_LOG.md` (step-by-step +
resume notes). See any folder's `README.md` for its own doc map.

## Reproduce any study

```bash
cd Research/token_comparison/<peer>      # e.g. mem0-comparison
python3 run_recall.py && python3 run_live.py && python3 run_bootstrap.py
python3 render_report.py && python3 render_report_detailed.py
```
Deterministic — re-running yields identical numbers (no network, no model calls).
Exact commands vary slightly per folder; see each folder's `README.md`.

## Conventions for a new peer study

1. Copy an existing peer folder as the template (keeps it self-contained).
2. Add `peers/<peer>_adapter.py` modeling that system from its **own published
   numbers** (so it can't be accused of sandbagging); set defaults to favor it.
3. Keep the **same tokenizer** (`tokencount.py`) and the **same frozen corpora +
   queries** — the cross-study fairness control.
4. Write a `CONTEXT.md`/`PROCESS_LOG.md` (step-by-step, so a later session resumes
   without rerunning) and keep numbers in committed `REPORT*.md`.
5. Add a row to the table above + to `MASTER_SUMMARY.md`.
