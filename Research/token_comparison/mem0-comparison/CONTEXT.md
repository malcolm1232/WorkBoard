# CONTEXT.md — read me first (full context for resuming)

**Purpose:** capture the whole story of this folder so a future session has full
context **without re-running anything**. If the session that built this has closed,
start here.

---

## 1. What this is

`Research/token_comparison/mem0-comparison/` is a standalone, reproducible benchmark
proving **WorkBoard** is more token-efficient than **mem0** on the **live memory
loop** (persist + recall as you work). **mem0 only** — no Letta, no claude-mem.

It exists because mem0 markets "90% fewer tokens" — but that's vs a *naive
full-context baseline*, not head-to-head against a structured peer. This runs the
missing head-to-head.

## 2. The headline result (this run)

- **Live loop, 100 sessions × 3 recalls:** WorkBoard **33.7% fewer model tokens**
  (719,700 vs 1,086,200) — mem0 spends an LLM extraction call every session; WB's
  carding is free.
- **Build memory:** WorkBoard **98.7% fewer** input tokens (64,162 vs 5,095,769),
  7× fewer model calls.
- **Recall vs full-context (26K):** WorkBoard saves **90.8%**, mem0 saves **93.1%**
  → WB matches mem0's own "90%."
- **HONEST — do NOT over-claim:** per *single* recall, mem0's flat ~1,800-tok bundle
  is **leaner** than WB's content-rich cards (2,399). WB wins the **loop** (free
  writes + 0 in-context memory), not the lookup. And WB's 306-tok/turn nudge must be
  trimmed (→40) to win all-in on long, recall-sparse sessions (breakeven ≈ 12 turns
  full / 89 trimmed).

## 3. How the numbers are produced (data flow)

```
queries.json (20 gold queries) ─┐
board_snapshot.json (frozen)  ──┤
corpora/{tiny,medium}/        ──┤
                                ▼
peers/workboard_adapter.py (REAL: card.py recall + harvest ingest)
peers/mem0_adapter.py      (MODELED: mem0's own published numbers)
                                ▼
run_recall.py  → results/raw/recall.json     (WB vs mem0 recall)
run_live.py    → results/raw/live.json       (PRIMARY: write + io-loop + crossover)
run_bootstrap.py → results/raw/bootstrap.json (build cost)
                                ▼
render_report.py          → REPORT.md
render_report_detailed.py → REPORT_DETAILED.md
```
Everything is deterministic: same inputs → byte-identical reports. No network, no
model calls (mem0 is modeled, WB recall is a local `card.py` read).

## 4. Step-by-step — how this folder was built (#749)

1. **Scaffolded** by copying the shared, peer-neutral harness out of the combined
   `../letta-comparison/` study: `tokencount.py`, `build_fixtures.py`,
   `corpus_stats.py`, `queries.json`, `lib/` (safety + read-only product copies),
   `board_snapshot.json`, `corpora/`, `.gitignore`.
2. **Copied only the mem0 + WorkBoard adapters** (`peers/mem0_adapter.py`,
   `peers/workboard_adapter.py`) — deliberately NOT claude_mem/letta adapters.
3. **Wrote fresh mem0-only drivers** (`run_recall.py`, `run_live.py`,
   `run_bootstrap.py`) and renderers (`render_report.py`,
   `render_report_detailed.py`) — 2-way (WorkBoard vs mem0), stripping every
   claude-mem / Letta column from the combined-study versions.
4. **Wrote docs:** `OVERVIEW.md` (explainer), `README.md` (usage + doc map), this
   `CONTEXT.md`.
5. **Ran + verified:** pipeline reproduces; both reports byte-identical on re-render;
   `lib/safety.py` confirms the live board is untouched.
6. **Notation fix:** the "vs full-context" column was rewritten from "−90.8%" to
   "90.8% fewer" (the minus read like a penalty; it's a *saving*, same direction as
   mem0's "90% fewer").

## 5. File map

| File | What it is |
|---|---|
| `OVERVIEW.md` | plain-English explainer (start here) |
| `README.md` | folder usage + doc map |
| `CONTEXT.md` | **this** — full story + resume |
| `REPORT.md` | headline report (auto-generated) |
| `REPORT_DETAILED.md` | exhaustive report (auto-generated) |
| `peers/workboard_adapter.py` | REAL WorkBoard ingest + recall + correctness |
| `peers/mem0_adapter.py` | mem0 modeled from its published numbers |
| `run_recall.py` / `run_live.py` / `run_bootstrap.py` | the 3 study drivers |
| `render_report.py` / `render_report_detailed.py` | the 2 renderers |
| `lib/safety.py` | non-invasiveness guard + snapshot fingerprint |
| `lib/card_ro.py` / `lib/product_scripts_ro/` | read-only copies of product code |
| `results/raw/*.json` | every computed number (git-ignored, present locally) |
| `board_snapshot.json` / `corpora/` | frozen inputs (git-ignored — private) |

## 6. Re-retrieve without re-running (cheap path)

The numbers already live in `results/raw/*.json` and both reports are written. To
re-read: open `REPORT_DETAILED.md`. To re-render from existing JSON (instant):
```bash
python3 render_report.py && python3 render_report_detailed.py
```
To fully re-derive (needs `~/.claude` for corpora):
```bash
python3 build_fixtures.py && python3 run_recall.py && python3 run_live.py \
  && python3 run_bootstrap.py && python3 render_report.py && python3 render_report_detailed.py
```

## 7. Gotchas / don't-over-claim

- The win is the **loop**, not per-recall (mem0 is leaner per single query).
- The per-turn nudge must be trimmed for long sessions (see crossover, REPORT §5.4 /
  REPORT_DETAILED §5.4).
- mem0 is **modeled** from its own best-case published numbers — not run (it needs
  an OpenAI key + Qdrant).
- "X% fewer" / "−X%" both mean a **reduction** — same direction as mem0's "90%".
- tiktoken ≈ 10–15% under Claude's real tokenizer, applied to both → ratios hold.
- `board_snapshot.json` + `corpora/` are git-ignored; a fresh clone re-derives them.
