# retrieval-bakeoff — which matcher recalls the right card, cheapest

Isolated, reproducible study (card #782) that picks WorkBoard's recall matcher by
**measuring** recall@k + tokens-per-correct over 20 gold queries, and benchmarks it
against how mem0 / claude-mem / Letta / graphify retrieve. The winner ships as
`card.py recall` (card #781).

**Start here:** [`REPORT.md`](REPORT.md) — findings, the head-to-head table, the
recommendation, and the honest catch.

## Layout
```
inputs/board_snapshot.json   FROZEN board (md5 7659518d…, 533 cards) — never the live board
inputs/queries.json          20 gold recall queries (pinpoint/thematic/lifecycle)
tokencount.py                shared tokenizer (tiktoken cl100k) — same as the cost studies
matchers/matchers.py         the 3 candidate matchers (lexical / BM25F / tf-idf cosine)
harness.py                   the bake-off → results/summary.txt + results/bakeoff_results.json
test_text_search.py          unit tests for the SHIPPED matcher (scripts/text_search.py)
peer_algos/                  source-level study of each peer's retrieval algorithm
METRICS.md                   metric definitions + international-benchmark calibration + honesty rules
REPORT.md                    the answer
results/                     generated output
```

## Reproduce
```
python3 harness.py            # deterministic; reads only the frozen snapshot
python3 test_text_search.py   # ALL PASS
```
No network, no model calls, no randomness — same inputs → same numbers.

## What ships
`scripts/text_search.py` (BM25F + literal/`#ref` + `expand_links` traversal) and
`card.py recall`. The shipped module reproduces this study's winning numbers
(hit@5 0.556, pinpoint hit@5 1.00). Nothing here mutates the product or the live
board — the only product files touched by the build are `scripts/text_search.py`,
the `recall` command in `scripts/card.py`, and docs (SKILL.md / README).
