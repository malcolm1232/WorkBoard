# graphify-comparison — WorkBoard vs Letta, mem0 & claude-mem efficiency study

Standalone, reproducible benchmark comparing **WorkBoard** to **Letta** (MemGPT),
**mem0**, and **claude-mem**, focused on the **live memory loop**.

- **Study 1 — Live loop (PRIMARY)**: steady-state cost of working with the system
  on — memory-WRITE (persist) + recall, plus an honest all-in crossover (mem0/claude-mem).
- **Study 1b — WorkBoard vs Letta (REAL)**: Letta's per-turn in-context memory tax,
  measured from Letta's own shipped prompts + tool schemas + `Memory.compile()`.
- **Study 2 — Recall**: tokens-to-recall across 20 gold queries.
- **Study 3 — Bootstrap (secondary)**: one-time cost to BUILD the memory.

The deliverable is **`REPORT.md`** (auto-generated); the exhaustive companion is
**`REPORT_DETAILED.md`**. **New here? Read `CONTEXT.md` first** — full story, file
map, and resume instructions. Cards #730 / #734 / #735 / #738.

**Peer measurement, honestly:** mem0 and claude-mem are modeled from each vendor's
OWN published numbers (can't be accused of sandbagging). **Letta is measured for
real** — it's the one peer cheap enough to run locally (Ollama backend, no key).
`letta_incontext_real.py` counts Letta's actual in-context payload (server- and
model-independent); `letta_real_run.py` adds optional live model-reported
corroboration against a local Letta server (Docker `letta/letta` + Ollama).

## Safety — in-repo but non-invasive

This folder now lives **inside** the repo (`WorkBoard/graphify-comparison/`) as a
tracked sub-project, but it **never mutates the rest of the product**: it reads
frozen copies and writes exclusively under this subfolder.

- Recall reads a **frozen copy** `board_snapshot.json`, never the live board.
- All product code is a **read-only copy** under `lib/product_scripts_ro/`
  (re-copy with `rsync` if the product extraction code changes).
- Ingest runs in a **throwaway `$HOME`** (symlinked fixture transcripts only).
- `lib/safety.py` **refuses** to write the live board (`board/board.json`) or
  product source outside this subfolder, and fingerprints the board snapshot.
- Verified: the live `board.json` is unchanged by running this study.

## Layout
```
graphify-comparison/
├── README.md            (this file)
├── REPORT.md            ← auto-generated study report (the deliverable)
├── tokencount.py        single shared tokenizer used for ALL counts (fairness)
├── build_fixtures.py    freezes ~/.claude/projects/*.jsonl → corpora/
├── corpus_stats.py      sessions / turns / transcript tokens per corpus
├── queries.json         20 questions × 3 shapes + gold answers (pre-written)
├── peers/
│   ├── workboard_adapter.py   ingest_estimate() + recall()  [REAL]
│   ├── mem0_adapter.py        live() + recall() + ingest_spec()  [mem0's own #s]
│   └── claude_mem_adapter.py  ingest_spec() + recall()      [claude-mem's own #s]
├── run_live.py          Study 1 — PRIMARY   → results/raw/live.json
├── run_recall.py        Study 2 — recall    → results/raw/recall.json
├── run_bootstrap.py     Study 3 — bootstrap → results/raw/bootstrap.json
├── render_report.py     regenerates REPORT.md from results/raw/*.json
├── lib/
│   ├── safety.py             non-invasiveness guard + snapshot fingerprint
│   ├── card_ro.py            read-only copy of product card.py
│   └── product_scripts_ro/   read-only copy of product scripts/ (ingest path)
├── corpora/             frozen transcript fixtures   [git-ignored — derive locally]
├── board_snapshot.json  frozen board copy            [git-ignored — may hold secrets]
└── results/raw/         machine-readable outputs      [aggregate ships, raw local]
```

## Reproduce

```bash
python3 build_fixtures.py     # freeze corpora from ~/.claude (once)
python3 run_recall.py         # Study 2 (3-way recall)
python3 run_live.py           # Study 1 (PRIMARY — live loop)
python3 run_bootstrap.py      # Study 3 (secondary — build cost)
python3 render_report.py      # regenerate REPORT.md
```

To re-freeze inputs deliberately (only when you intend to):
`cp ~/Desktop/WorkBoard/board/board.json board_snapshot.json` and
`rsync -a --exclude __pycache__ ~/Desktop/WorkBoard/scripts/ lib/product_scripts_ro/`

## Fairness rules (enforced by the harness, not just promised)

1. **Same tokenizer** (`tokencount.py`, tiktoken cl100k) for every count, all 3 systems.
2. **Same corpus** — `corpora/<size>/` is byte-frozen + fingerprinted by `build_fixtures.py`.
3. **Peers measured by their OWN published numbers** (mem0: arXiv:2504.19413 +
   mem0.ai/research-3; claude-mem: its README) so neither can be accused of
   sandbagging; defaults FAVOR the peers (mem0's flat 1.8K recall is its best case).
4. **Gold answers** in `queries.json` written before either system was queried.
5. **Correctness is real** — a WorkBoard answer counts only if every gold fact literally appears in a fetched card.
6. **Honest crossover published** — the per-turn nudge is WorkBoard's heavier
   surface; Study 1 §4 shows where mem0 wins, not just where WorkBoard does.
7. **Raw outputs** in `results/raw/` + deterministic re-runs so any number in REPORT.md is re-derivable.

## Headline (this run)

- Live loop, 100 sessions × 3 recalls: **WorkBoard ~34% fewer model tokens than
  mem0**, ~53% fewer than claude-mem — because mem0 pays an LLM extraction call
  every session and WorkBoard's carding is free.
- Recall vs full-context: **WorkBoard −90.8%**, matching mem0's own “90%” headline.
- Honest: mem0's flat ~1.8K per-query retrieval is leaner than WorkBoard's cards;
  WorkBoard's per-turn nudge must be trimmed (→~40 tok) to win all-in on long sessions.

## Inactivity window

User was away **2026-06-11 → 2026-06-15** (5 days). Fixtures avoid straddling
that gap so "tokens per day" stays interpretable. `large` ends 2026-06-10.
