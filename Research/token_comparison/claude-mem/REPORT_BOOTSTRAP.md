# Study A — Bootstrap: cost to BUILD the memory

> Auto-generated from `results/raw/bootstrap.json`. Tokenizer `tiktoken-cl100k_base` (identical for both systems). Card #730. Part of the WorkBoard vs claude-mem study (`REPORT.md`).

## The question

Given the **same corpus** of past Claude-Code transcripts, how much does each system spend — model calls and model-input tokens — to turn that history into a recall-ready memory store? This is the one-time (or per-onboarding) cost a user pays to *populate* memory from existing work.

## Method

- **Corpus:** three frozen snapshots of real transcripts from `~/.claude/projects`, fingerprinted in `corpora/<size>/manifest.json`, excluding the 2026-06-11→15 inactivity gap.
- **WorkBoard = measured for real.** We run the actual bootstrap harvest + bucketize path (`scripts/hourly_extractor.py`, imported read-only) inside a sandboxed `$HOME` whose `.claude/projects` holds only the fixture. We count the hourly buckets it forms (= one Haiku call each, `chunk-size 1`) and tokenize the **compact per-bucket digest** it actually feeds the model.
- **claude-mem = its own design.** Its SessionEnd hook compresses each session by reading that session's **full transcript** through one Agent-SDK call. So its model calls = number of sessions, and its model-input tokens = the transcript tokens (measured on the same corpus, same tokenizer).
- **What we are NOT counting:** the deterministic, no-model harvest both systems do to read files off disk. We count only what reaches a model — the tokens you pay for.

## Results — per corpus

| Corpus | Window | Sessions | Turns | WB model calls | claude-mem calls | Fewer calls | WB input tok | claude-mem input tok | **Input reduction** |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| tiny | 2026-06-16→2026-06-17 | 336 | 2,112 | 17 | 336 | 19.8× | 1,868 | 1,481,053 | **99.9%** |
| medium | 2026-05-28→2026-06-10 | 933 | 11,693 | 88 | 933 | 10.6× | 10,546 | 5,095,769 | **99.8%** |
| large | 2026-05-17→2026-06-10 | 984 | 16,245 | 144 | 984 | 6.8× | 18,522 | 5,694,890 | **99.7%** |

## Where each number comes from

**tiny** (336 sessions over 2026-06-16→2026-06-17):
- WorkBoard groups the work into **17 hourly buckets** → 17 Haiku calls, averaging **110 tok/call** of compact digest = 1,868 model-input tokens.
- claude-mem compresses all **336 sessions** → 336 calls, averaging **4,408 tok/call** (the full session transcript) = 1,481,053 model-input tokens.
- Net: **19.8× fewer calls**, **99.9% fewer model-input tokens**.

**medium** (933 sessions over 2026-05-28→2026-06-10):
- WorkBoard groups the work into **88 hourly buckets** → 88 Haiku calls, averaging **120 tok/call** of compact digest = 10,546 model-input tokens.
- claude-mem compresses all **933 sessions** → 933 calls, averaging **5,462 tok/call** (the full session transcript) = 5,095,769 model-input tokens.
- Net: **10.6× fewer calls**, **99.8% fewer model-input tokens**.

**large** (984 sessions over 2026-05-17→2026-06-10):
- WorkBoard groups the work into **144 hourly buckets** → 144 Haiku calls, averaging **129 tok/call** of compact digest = 18,522 model-input tokens.
- claude-mem compresses all **984 sessions** → 984 calls, averaging **5,787 tok/call** (the full session transcript) = 5,694,890 model-input tokens.
- Net: **6.8× fewer calls**, **99.7% fewer model-input tokens**.

## Why the gap is so large

Two compounding effects:

1. **Unit of capture.** WorkBoard captures *work*, bucketed by the hour — a busy day is a handful of buckets. claude-mem captures *every session*, and a dense corpus has many short sessions (this corpus: hundreds in days). Calls scale with sessions for claude-mem, with active hours for WorkBoard.
2. **Input shape.** WorkBoard's harvest pre-distills each bucket into a compact digest (a few hundred tokens) before the model sees it. claude-mem feeds the **whole transcript** to its compression model. So even per call, WorkBoard's model-input is an order of magnitude smaller.

The harvest+digest WorkBoard does first is deterministic and model-free — it shifts the heavy lifting off the paid model and onto cheap local code.

## Illustrative cost

> Assumption: **$1.00 / million input tokens** (Haiku-tier; change the rate, the ratio is unchanged). Input-only; both also emit output, but input dominates ingestion.

| Corpus | WorkBoard $ | claude-mem $ | You save |
|---|--:|--:|--:|
| tiny | $0.0019 | $1.4811 | 99.9% |
| medium | $0.0105 | $5.0958 | 99.8% |
| large | $0.0185 | $5.6949 | 99.7% |

## Honest caveats

- **Different artifacts.** claude-mem's per-session compression yields rich semantic observations of the *whole conversation*; WorkBoard's digest extracts *work outcomes* (cards). claude-mem captures more raw conversational detail — it spends more to store more. The study measures cost-to-build, not information equivalence.
- **claude-mem numbers are from its design + the same corpus**, not a live run of its tool. A sandboxed real-run validation is scripted in `run_claude_mem_tiny.md`; the spec uses claude-mem's own mechanics so it can't be accused of sandbagging.
- **WorkBoard is the real bootstrap path**, measured, but token counts use the `cl100k` proxy tokenizer (≈10-15% under Claude's true count) — applied identically to both, so the ratio holds.

## Reproduce

```bash
cd ~/Desktop/claude-mem-comparison
python3 build_fixtures.py     # freeze corpora from ~/.claude (once)
python3 run_bootstrap.py      # writes results/raw/bootstrap.json
python3 report_bootstrap.py   # regenerate this file
```
Sandboxed: ingest runs in a throwaway `$HOME`; the product bootstrap code is imported read-only and never modified.

## Raw data

```json
{
  "tokenizer": "tiktoken-cl100k_base",
  "fixtures": [
    {
      "corpus": "tiny",
      "window": [
        "2026-06-16",
        "2026-06-17"
      ],
      "sessions": 336,
      "turns": 2112,
      "transcript_tokens": 1481053,
      "wb_model_calls": 17,
      "cm_model_calls": 336,
      "wb_ingest_input_tokens": 1868,
      "cm_ingest_input_tokens": 1481053,
      "calls_ratio_cm_over_wb": 19.8,
      "input_reduction_pct": 99.9,
      "wb_buckets": 17
    },
    {
      "corpus": "medium",
      "window": [
        "2026-05-28",
        "2026-06-10"
      ],
      "sessions": 933,
      "turns": 11693,
      "transcript_tokens": 5095769,
      "wb_model_calls": 88,
      "cm_model_calls": 933,
      "wb_ingest_input_tokens": 10546,
      "cm_ingest_input_tokens": 5095769,
      "calls_ratio_cm_over_wb": 10.6,
      "input_reduction_pct": 99.8,
      "wb_buckets": 88
    },
    {
      "corpus": "large",
      "window": [
        "2026-05-17",
        "2026-06-10"
      ],
      "sessions": 984,
      "turns": 16245,
      "transcript_tokens": 5694890,
      "wb_model_calls": 144,
      "cm_model_calls": 984,
      "wb_ingest_input_tokens": 18522,
      "cm_ingest_input_tokens": 5694890,
      "calls_ratio_cm_over_wb": 6.8,
      "input_reduction_pct": 99.7,
      "wb_buckets": 144
    }
  ]
}
```
