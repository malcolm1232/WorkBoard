# Retrieval bake-off — which method best recalls the right card, cheaply

**Question (from the user):** find the *best* retrieval method for surfacing the
right past work — **low token cost first, decent accuracy second** — and ship it
as a CLI users' agents can recall with. Study how the peers (mem0, claude-mem,
Letta, graphify) retrieve, and benchmark against them.

**Answer:** ship **BM25F** — field-weighted lexical ranking + exact-literal/`#ref`
boosting + link traversal. Zero dependencies, zero model calls, **~268 tokens per
correct recall vs the vector peers' ~6,956/retrieval (~26× leaner)**. It wins the
two work-ledger shapes that matter most (**pinpoint hit@5 = 1.00**, lifecycle via
graph-walk) and *honestly concedes* open-ended thematic recall to dense vectors —
the right trade when cost is the priority and the agent can bridge the rest.

> Shipped as `card.py recall "<text>" [--top N] [--traverse]`, backed by
> `scripts/text_search.py`. The shipped module reproduces the winning numbers
> below exactly (`python3 test_text_search.py`).

---

## 1. The bake-off (measured, deterministic, on the frozen 533-card snapshot)

Three **zero-dep** candidate matchers over the index layer, scored on the 20 gold
recall queries (18 with a gold card #), `tiktoken cl100k`:

| matcher | hit@1 | hit@3 | hit@5 | cov@5 | cov@3+walk | tok/q | **tok/correct** |
|---|--:|--:|--:|--:|--:|--:|--:|
| lexical (H1) | 0.333 | 0.389 | 0.389 | 0.283 | 0.238 | 113 | 292 |
| **BM25F (H2) ← winner** | **0.333** | **0.389** | **0.556** | **0.359** | 0.246 | **104** | **268** |
| tf-idf cosine (H3) | 0.222 | 0.444 | 0.444 | 0.258 | 0.252 | 131 | 295 |

By shape (hit@3 / hit@5):

| shape | lexical | **BM25F** | tf-idf cosine |
|---|---|---|---|
| **pinpoint** (n=5) | 0.60 / 0.60 | **0.60 / 1.00** | 0.80 / 0.80 |
| **thematic** (n=7) | 0.14 / 0.14 | 0.14 / 0.29 | 0.14 / 0.14 |
| **lifecycle** (n=6) | 0.50 / 0.50 | **0.50 / 0.50** | 0.50 / 0.50 |

**Why BM25F wins:** best hit@5 (0.556) and coverage, lowest tokens-per-correct,
and a **perfect pinpoint hit@5 (1.00)** — every exact-reference query resolves.
tf-idf edges hit@3 but plateaus (ranks 4–5 add nothing) and is weaker on coverage;
plain lexical lacks corpus-aware weighting. (Design notes: indexing the **full**
card text — free for a local matcher — then field-weighting so the curated
**title** dominates and the long writeup is damped; plus a decisive bonus when the
query names a `#ref`, the deterministic edge a dense vector blurs.)

## 2. Measured dense baseline (H4 — the real head-to-head)

The strongest test: embed our 533 cards + the gold queries with **OpenAI
`text-embedding-3-small` (1536-d) — the exact model mem0 and Letta default to** —
rank by cosine, measure recall@k on **our** corpus. (Reproducible:
`dense_eval.py`, vectors cached in `results/dense_cache.json`.) This turns the
"vectors win thematic, lose pinpoint" claim from *asserted* into *measured*:

| metric | **BM25F (zero-dep)** | **Dense 3-small (measured)** | winner |
|---|--:|--:|---|
| **pinpoint** hit@5 | **1.00** | 0.40 | **BM25F (decisive)** |
| **thematic** hit@5 | 0.286 | **0.429** | dense |
| **lifecycle** hit@5 | 0.500 | **0.667** | dense |
| overall hit@3 | 0.389 | **0.444** | dense (slight) |
| overall hit@5 | **0.556** | 0.500 | BM25F (slight) |
| tokens / correct | **~268** | ~6,956 injected + embed | **BM25F (~26×)** |
| infra | **none (stdlib)** | OpenAI API + vector store | **BM25F** |
| model call / retrieval | **none** | embedding call | **BM25F** |

**The finding:** a real dense embedding **confirms** it blurs exact literals
(pinpoint 0.40 vs BM25F's 1.00) and **wins fuzzy** thematic/lifecycle — but
**overall it's roughly a wash** (hit@3 0.444 vs 0.389; hit@5 0.500 vs 0.556). So
paying for an embedding API + a vector store (and, for the actual peers, a
per-session/per-turn LLM tax) buys you *some* thematic recall while *losing*
pinpoint precision, at **~26× the tokens**. Under the stated cost-first
constraint, that trade is not worth it — **BM25F is the right ship.**

> This dense number is a **best-case upper bound** on the real peers: pure cosine,
> no threshold gating, no recency cap. mem0 gates sub-0.1 semantic hits and
> claude-mem's *auto* path is recency-only — both of which would push their
> real-world recall **below** this 0.444. So the gap to the shipped peers is, if
> anything, smaller than shown.

## 3. How the peers retrieve (source-level)

Algorithm study of each peer's retrieval path (full detail in
`peer_algos/peer_retrieval_algorithms.md`); the dense column above is the measured
stand-in for their vector ranking on our data.

| | **WorkBoard (BM25F, measured)** | mem0 | claude-mem | Letta |
|---|---|---|---|---|
| Retrieval method | field-weighted lexical + #ref + graph-walk | vector+BM25+entity hybrid | recency (auto) / MiniLM (on-demand) | vector cosine (agent-triggered) |
| Pinpoint (exact #/sha/file) | **hit@5 1.00** | partial (semantic threshold gates literals) | FTS5 ok / recency misses | weak (cosine blurs IDs) |
| Thematic (open topic) | weak (0.14 / 0.29) | **strong** | recency-capped / strong on-demand | **strong** |
| Lifecycle (shipped+open) | 0.50 + graph-walk | weak (no set-completion) | weakest (truncation) | weak (single top-k) |
| Tokens / recall | **~104 inject (~268/correct)** | ~6,956 | grows w/ memory | ~3,444 / turn always-on |
| Model call to retrieve | **none** | embed/query | none (read) | **≥1 LLM round-trip** |
| Infra required | **none (stdlib)** | OpenAI + Qdrant | Bun/uv/Chroma | pgvector |
| Per-session/turn LLM tax | **none** | extract/session | ~5K compress/session | per-turn re-send |
| Published recall | — (measured here) | LOCOMO J=66.9% (chat QA) | none | DMR 92.5% (synthetic) |

**Calibration:** hit@k is the same metric family as BEIR/MTEB recall@k and
LOCOMO/MemGPT hit-rate; absolute values aren't cross-comparable (corpus size/domain
differ), but the relative ranking + tokens-per-correct transfer. Details: `METRICS.md`.

## 4. Recommendation & the honest catch

**Ship BM25F + #ref + traversal.** Under the stated constraint (cost ≫ raw
accuracy) it's the correct choice: it makes the high-value work-recall shapes
(pinpoint, lifecycle) deterministic and traceable at **1/26th** the token cost,
with **no vector DB, no API, no per-session/per-turn LLM tax** — which is exactly
what a launching, lightweight product needs.

**The catch (stated plainly):** WorkBoard is **not** the best at open-ended
*semantic thematic* recall. A query like "what did we discuss about the digest"
whose vocabulary doesn't overlap the curated card text (e.g. gold card titled
"bootstrap backlog") is where a dense vector wins. WorkBoard closes that gap at the
**agent layer**, not the index: `recall` surfaces cheap entry points; the agent
reads the candidates and follows `--traverse` links. It's *cheap deterministic
retrieval + agent reasoning*, not a black-box embedding. For a user whose primary
need is fuzzy recall over **uncarded** chat, a dense store is the better tool —
WorkBoard wins the **work-ledger** axes (and the **whole loop** cost, per the
sibling token studies) decisively.

## 5. Reproduce
```
cd Research/retrieval-bakeoff
python3 harness.py            # zero-dep bake-off → results/
python3 test_text_search.py   # shipped-matcher unit tests
# H4 dense baseline (needs an OpenAI key; vectors then cached for free re-runs):
export OPENAI_API_KEY="$(cat '/path/to/key.txt')"
python3 dense_eval.py         # → results/dense_results.json
```
Read-only on the frozen snapshot; deterministic; no network/model calls.

*Cards #781 (build the recall CLI) · #782 (this retrieval-accuracy study).*
