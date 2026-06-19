# global-benchmarks — WorkBoard's recall CLI on the world's benchmarks (card #784)

Puts the **exact BM25 ranking core** that ships in `scripts/text_search.py` on the
standard public benchmarks used to rank retrievers and models, and measures a dense
baseline (OpenAI `text-embedding-3-small`) on the *same* data. Answers: *how does
this method fare against "intentional global models"?*

**Start here:** [`REPORT.md`](REPORT.md) — the one cross-benchmark table + the
honest "vs Claude/ChatGPT" framing.

## Benchmark families
| Family | Dataset(s) | Metric | Script |
|---|---|---|---|
| Embedding/retrieval IR | BEIR SciFact, NFCorpus | nDCG@10, Recall@k | `beir_eval.py` (BM25), `beir_dense.py` (dense) |
| Agent/conversational memory | LOCOMO (10 convs, 1.5K Qs) | Recall@k by category | `locomo_eval.py [--dense]` |
| Long-context | synthetic NIAH | top-1 across depth×size | `niah_demo.py` |

## Reproduce
```
python3 beir_eval.py scifact          # → results/beir_scifact.json  (also nfcorpus)
export OPENAI_API_KEY="$(cat '/Users/malco/Desktop/temp throwaway key.txt')"
python3 beir_dense.py scifact         # dense, same data
python3 locomo_eval.py --dense        # → results/locomo.json
python3 niah_demo.py                  # → results/niah.json
```
Stdlib + numpy + requests. Datasets cached in `inputs/` (BEIR zips from the public
UKP mirror; LOCOMO from snap-research/locomo). Embedding vectors cached in
`results/` — the **API key is read from env and never written to disk**.

## Headline (measured)
- Our BM25 reproduces canonical BEIR BM25 (SciFact **0.658** vs published 0.665).
- Dense beats BM25 on prose/chat by ~7–13 pts (SciFact 0.730, LOCOMO R@5 0.613).
- On the structured work-ledger (#782), BM25F **wins** (pinpoint hit@5 1.00 vs 0.40).
- NIAH single-needle: **100%** for ~0 tokens. RULER reasoning = the model's job, not a retriever's.

Nothing here mutates the product or the live board; it only *evaluates* the shipped matcher.
