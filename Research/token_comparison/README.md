# memory-benchmarks — WorkBoard vs memory systems

Benchmark suite comparing **WorkBoard** against AI-memory systems on token
efficiency. Lives under `dev/` so it's **git-ignored and never ships** (the
repo's designated home for "tests, sims, benchmarks"). Nothing here touches the
live product: each study reads a frozen `board_snapshot.json` and runs in a
sandboxed `$HOME`.

Each peer is its **own self-contained, independently-reproducible folder** — you
can run any one of them alone, or zip it and hand it to someone.

## Peers

| Study | Status | Headline (vs WorkBoard) |
|---|---|---|
| **[claude-mem/](claude-mem/)** | ✅ done (#730) | Bootstrap **98.6–99.2% fewer** ingest tokens; recall **25.9% fewer** (33% lifecycle); live **0 vs 546K** model tokens/100 sessions |
| **[mem0-letta/](mem0-letta/)** | ✅ done (#734/#735) | Combined live-loop study (WorkBoard vs **mem0 + claude-mem + Letta**). mem0: live loop **33.7% fewer** tokens, recall vs full-context **−90.8%** (≈ mem0's "90%"). Letta: **81.0% fewer** (92.2% trimmed) — Letta's tax is per-*turn* (re-sent core blocks + tool schemas). |
| **graphify** | ⏳ deferred (#733) | — |

> Numbers from each study's own auto-generated reports. claude-mem/mem0/letta are
> measured/modeled per each study's `REPRODUCIBILITY` notes; settings favor the
> peer, so WorkBoard margins are conservative floors.

## Layout

```
Research/token_comparison/        (tracked; private data git-ignored — see .gitignore)
├── README.md                     (this index)
├── claude-mem/                   deep-dive study — see its README/PROCESS_LOG
│   ├── REPORT_FULL.md  PROCESS_LOG.md  REPRODUCIBILITY.md  REAL_RUN_FINDINGS.md
│   ├── peers/  run_*.py  report_*.py  tokencount.py  queries.json
│   ├── lib/product_scripts_ro/   vendored read-only product scripts (git-ignored)
│   ├── board_snapshot.json  corpora/  results/  reference/   (git-ignored)
├── mem0-letta/                   combined live-loop study (mem0 + claude-mem + Letta)
│   ├── CONTEXT.md  REPORT.md  REPORT_DETAILED.md  run_live*.py  letta_*.py
│   ├── peers/{mem0,claude_mem,letta,workboard}_adapter.py
│   └── lib/{safety.py,card_ro.py,product_scripts_ro/}        (vendored git-ignored)
└── graphify/                     (when #733 is done)
```

> Two folders both touch claude-mem by design: `claude-mem/` is the deep
> bootstrap+recall+real-run dive; `mem0-letta/` uses claude-mem as one baseline
> in the multi-peer live-loop comparison. Kept whole (not split per-peer) to
> preserve the working combined harness.

## Reproduce any study

```bash
cd dev/memory-benchmarks/<peer>
python3 run_bootstrap.py && python3 run_recall.py && python3 replay_session.py
# then the report generators (see that study's README)
```

## Conventions for a new peer study

1. Copy an existing peer folder as the template (keeps it self-contained).
2. Add `peers/<peer>_adapter.py` modeling that system from its **own published
   numbers** (so it can't be accused of sandbagging); set defaults to favor it.
3. Keep the **same tokenizer** (`tokencount.py`) and the **same frozen corpora +
   queries** — that's the cross-study fairness control.
4. Write a `PROCESS_LOG.md` (step-by-step) so a later session resumes without
   rerunning, and a `REPRODUCIBILITY.md` (per-number provenance).
