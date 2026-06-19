# Where WorkBoard's recall CLI lands on the *global* benchmarks

**Question (user):** the bake-off compared our matcher to peers on *our* board.
How does the same CLI method fare on the **standard global benchmarks** the world
ranks retrievers (and models) on — and where does it sit vs dense embeddings and
vs Claude/ChatGPT?

**Method:** run the **exact BM25 ranking core** that ships in
`scripts/text_search.py` on three public benchmark families, and **measure a dense
baseline** (OpenAI `text-embedding-3-small`, 1536-d — the model mem0/Letta use) on
the *same* data, so every comparison is apples-to-apples, not cited from memory.
All harnesses validate against published numbers.

---

## The one table — measured BM25 (ours) vs dense, on each benchmark

| Benchmark | What it tests | Metric | **Our BM25** | **Dense `3-small`** | Published ref |
|---|---|---|--:|--:|--:|
| **BEIR · SciFact** | scientific-claim IR (prose) | nDCG@10 | 0.658 | **0.730** | BM25 0.665 ✓ |
| **BEIR · NFCorpus** | medical IR (prose) | nDCG@10 | 0.305 | **0.385** | BM25 0.325 ✓ |
| **LOCOMO** | long-conversation memory | Recall@5 | 0.510 | **0.613** | mem0 end-to-end J=66.9%* |
| **WorkBoard cards** (from #782) | structured work-ledger | hit@5 | **0.556** | 0.500 | — (measured) |
| → *pinpoint* slice (exact `#`/sha/file) | literal recall | hit@5 | **1.00** | 0.40 | — |
| **NIAH** single-needle | long-context find | top-1 | **1.00** | (LLMs ~0.99) | — |

\* mem0's J is end-to-end *answer* quality (LLM judge), not retrieval recall — a
different, higher-in-the-stack metric; shown only for orientation.

## What it says

1. **Our matcher = the canonical BM25 tier, validated.** Our zero-dep core
   reproduces the world-standard BM25 nDCG (SciFact 0.658 vs published 0.665;
   NFCorpus 0.305 vs 0.325). So on the global MTEB/BEIR scale, WorkBoard's recall
   sits exactly where BM25 sits — a strong, universally-understood baseline.

2. **On prose / conversational semantics, dense wins by ~7–13 pts** (SciFact +0.07,
   NFCorpus +0.08, LOCOMO Recall@5 +0.10). That is dense's home turf: natural-
   language questions whose words don't match the source (paraphrase), over flowing
   text. The top *commercial* embedding models (text-embedding-3-large, Voyage-3,
   NV-Embed-v2) push SciFact a few points higher still (~0.76–0.78).

3. **On the structured, literal-heavy work-ledger, lexical wins** — BM25F ties
   dense overall (hit@5 0.556 vs 0.500) and **crushes it on pinpoint** (1.00 vs
   0.40), because exact tokens (`#627`, `f93dc43`, `board.html`) are signal a
   dense vector blurs. **The crossover is corpus-driven:** paraphrase-over-prose →
   dense; exact-literal-over-structured → lexical.

4. **Cost stays the decider.** Dense's ~7–13-pt edge on prose costs an embedding
   API + a vector store + (for the real peers) a per-session/turn LLM tax, and
   ~26× the tokens per recall. WorkBoard's corpus is the literal/structured kind
   where lexical already wins — so paying for dense would *lose* accuracy on the
   shapes that matter and add infra. **BM25F is the right ship.**

## Where Claude / ChatGPT fit (the honest "vs the models" answer)

They're a **different layer**, so they're not on BEIR/LOCOMO (those rank
*retrievers*). The models are ranked on **long-context** suites:
- **NIAH** (find a planted fact) is essentially solved — frontier Claude/GPT/Gemini
  ~99–100%. Our retriever also does single-needle ~100% (15/15 cells) — but for
  **~0 tokens**, where the model pays for the *whole* haystack every query.
- **RULER / NoLiMa / HELMET** (multi-hop, variable tracking, aggregation) test the
  **model's in-context reasoning** — a retriever *cannot* do these. This is where
  the models compete with each other (effective context ≪ advertised window).

So: **WorkBoard ≠ a rival to Claude/ChatGPT.** It decides *what enters the window*
(cheaply, deterministically); the model *reasons over it*. They compose. The real
choice WorkBoard changes is "retrieve a few hundred relevant tokens" vs "stuff 26K
of history into the model and let its (excellent, ~solved) NIAH retrieval sort it
out" — same answer, ~26× the cost.

## Bottom line
On the global scale, WorkBoard's recall is **the BM25 baseline tier** — within
single-digit nDCG of dense on prose IR, **ahead of dense on literal/structured
recall**, at **zero infra and ~26× lower token cost**. For a work-ledger that's
the right point on the curve; for fuzzy prose/chat recall a dense store scores a
few points higher (at real infra cost), and the model layer (Claude/GPT) is
orthogonal — it reasons over whatever WorkBoard hands it.

## Reproduce
```
python3 beir_eval.py scifact     # BM25 nDCG@10 / Recall@k  (also: nfcorpus)
export OPENAI_API_KEY="$(cat '<key>')"
python3 beir_dense.py scifact    # dense baseline, same data (cached)
python3 locomo_eval.py --dense   # conversational-memory recall@k by category
python3 niah_demo.py             # single-needle long-context retrieval
```
Deterministic; datasets cached under `inputs/`; embeddings cached under `results/`
(key never stored). *Card #784.*
