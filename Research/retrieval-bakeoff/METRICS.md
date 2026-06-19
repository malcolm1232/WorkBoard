# Metrics & how they map to international benchmarks

## What we measure, and why

The goal is the **best retrieval method under a token constraint** — low cost
first, decent accuracy second. So we report an *accuracy* axis and a *cost* axis,
then combine them.

| Metric | Definition | Why it's the right metric |
|---|---|---|
| **hit@k** (a.k.a. recall@k / hit-rate) | fraction of queries where ≥1 gold card # appears in the top-k surfaced cards | The recall CLI's job is to surface a true **entry point** the agent can walk from; "did a correct card make the top-k" is exactly that. Standard IR metric (BEIR, MTEB, LOCOMO all report hit-rate / recall@k). |
| **gold-coverage@5** | mean fraction of *all* gold cards (per query) in the top-5 | Captures *completeness*, which matters for multi-card **lifecycle** answers — the axis pure similarity-ranking is weak on. |
| **gold-coverage@3 + traverse** | coverage after expanding the top-3 along `linkedCards` (1 hop) | Measures WorkBoard's **structural** recall — reaching the rest of a story by walking the graph, which a vector top-k cannot do. |
| **mean tokens/query** | `tiktoken cl100k` tokens of the `<board-steward-recall>` block injected for the top-3 (titles + #ref only) | The thing the user pays. Detail is pulled on demand, so this is the *whole* injected cost. |
| **tokens-per-correct@3** | mean tokens ÷ hit@3 | The single efficiency number — cost normalised by accuracy. Directly comparable to mem0's published "~6,956 tokens per retrieval". |

**Scope.** 18 of the 20 gold queries have ≥1 gold **card #** (P03, P06 have file-path/dir gold answers, not cards) → they're excluded from card-recall and noted. Queries span 3 shapes: **pinpoint** (5), **thematic** (7), **lifecycle** (6).

**Tokenizer.** `tiktoken cl100k_base` — the same tokenizer used across all four
prior token-cost studies, so cost numbers are directly comparable to them.

## Calibration to international benchmarks

| Benchmark | What it reports | How ours relates |
|---|---|---|
| **BEIR / MTEB (retrieval)** | nDCG@10, Recall@k over standard IR corpora | Our **hit@k / coverage@k** are the same *family* of metric (rank-based recall). Absolute values are **not** cross-comparable — BEIR corpora are 100K–1M+ generic docs; ours is 533 curated, domain-specific cards. The **relative** matcher ranking (BM25F > tf-idf > lexical) and the **tokens-per-correct** axis are the transferable results. |
| **LOCOMO** (mem0's benchmark) | LLM-as-Judge **J**, F1, BLEU-1 over multi-session *conversational* QA | mem0's headline **J=66.9%** is an *answer-quality* judge on chat dialogues — a different task and corpus from "surface the right work card". We cite it as the peer's own best-case number, **not** a number measured on our corpus. |
| **MemGPT paper (DMR)** | accuracy / ROUGE-L on a synthetic deep-memory set | Same caveat — synthetic, not curated work cards. Cited as Letta's published best case. |

**What is measured vs modeled (stated up front).**
- **WorkBoard's matchers** (lexical / BM25F / tf-idf) — **fully measured**,
  deterministic, zero-dep (`harness.py`).
- **Dense baseline (H4)** — **MEASURED** with OpenAI `text-embedding-3-small`
  (1536-d, the model mem0/Letta default to), via a throwaway key, on our corpus
  (`dense_eval.py`; vectors cached, key never stored). This is pure cosine —
  a **best-case upper bound** on the peers (no threshold gating, no recency cap).
- **Still modeled** — the peers' *full pipelines* (mem0's 0.1 semantic gate +
  entity boost; claude-mem's recency-only auto path; Letta's agent-triggered
  round-trip + per-turn tax) and their *published* benchmark numbers (LOCOMO J,
  MemGPT DMR), which are on different corpora and cited as such. These wrappers
  only *lower* real-world recall below the measured dense upper bound.

WorkBoard itself still needs **no** embedding infra to be evaluated or run — the
dense baseline required a paid API; the WorkBoard column did not.

## Reproducibility
Everything reads the **frozen** `inputs/board_snapshot.json`
(md5 `7659518d…`, 533 cards) — never the live board. Re-run:
```
python3 harness.py            # the bake-off → results/summary.txt + bakeoff_results.json
python3 test_text_search.py   # unit tests for the shipped matcher
```
Deterministic: no randomness, no network, no model calls. Same inputs → same numbers.
