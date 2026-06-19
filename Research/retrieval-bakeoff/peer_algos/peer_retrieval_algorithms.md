# Peer retrieval algorithms — how each system finds the right memory

Source-level study of each peer's **retrieval path** (not its write/extraction
path), from the live GitHub repos (June 2026). This is what we benchmark
WorkBoard's matcher against. Key takeaway up front:

> Every memory peer is **dense-vector-first**. That makes them strong on
> open-ended *thematic* recall and structurally weak on the two shapes a work
> ledger needs most — **pinpoint** (exact `#627`/`f93dc43`/`board.html`, which an
> embedding blurs) and **lifecycle** (set-completion "what shipped + what's
> open", which similarity-ranking cannot do). They also each pay an LLM/infra tax
> WorkBoard does not.

---

## mem0 (`mem0ai/mem0`)
- **Ranking:** HYBRID additive scorer (`mem0/utils/scoring.py:score_and_rank`): vector cosine (semantic) + **BM25** keyword (sigmoid-normalised) + **entity-match boost** (regex entities, weight ≤0.5), combined `min((sem+bm25+ent)/max_possible, 1)`. The **threshold (0.1) gates the *semantic* score BEFORE combining** — BM25/entity cannot rescue a sub-threshold vector hit.
- **Embeddings/store:** `text-embedding-3-small` (1536-d) → **Qdrant**, cosine. `top_k=20`, mandatory user/agent/run filter.
- **Cost:** retrieval is **LLM-free** in OSS default (no query-rewrite/rerank unless configured), but needs **1 embedding call/query** + Qdrant, and a **per-session LLM extraction call** on the write side. Platform figure: **~6,956 tokens injected per retrieval**.
- **By shape:** thematic **strong**; pinpoint *helped* by BM25+entity but semantic-threshold gating can still drop exact-literal hits; lifecycle **weak** (returns 20 most-similar, not an exhaustive open/closed partition).
- **Published:** LOCOMO LLM-as-Judge **J=66.9%** (vs OpenAI-memory 52.9%), ~90% fewer tokens vs full-context. Dataset = multi-session *conversational* QA → does not transfer to a curated card corpus.

## claude-mem (`thedotmack/claude-mem`)
- **Two distinct paths, easy to conflate:**
  1. **Automatic injection** (the default UX, every session/prompt) = **pure SQLite recency** (`ObservationCompiler`: `ORDER BY created_at_epoch DESC LIMIT 50` + 10 summaries). It passes **only the project name**, not the prompt — so it is **NOT prompt-aware semantic search**; old relevant memories outside the recency window are simply never surfaced.
  2. **On-demand search** (only when the agent explicitly invokes the `search` MCP tool) = Chroma vector (`all-MiniLM-L6-v2`, 384-d) ∩ SQLite metadata, FTS5 fallback.
- **Cost:** retrieval is **LLM-free**; the **~5K-token compression LLM call is per-session on write**. Needs Bun/uv/Node22 + Chroma worker.
- **By shape:** auto path **fails any non-recent recall**; on-demand FTS5 is decent for literals, MiniLM weak on opaque IDs; lifecycle **weakest** (recency truncation, no completeness). **No published recall/precision numbers** (only a code-exploration token-savings benchmark).

## Letta / MemGPT (`letta-ai/letta`)
- **Two tiers:** *core memory* blocks **re-sent in the system prompt every turn**; *archival* memory searched on demand.
- **Ranking (archival):** default NATIVE = **pure vector cosine** (pgvector `cosine_distance`); opt-in Turbopuffer = hybrid vector+BM25 fused via **RRF (k=60)**. Triggered by the LLM **calling `archival_memory_search`** → an extra inference round-trip per recall.
- **Embeddings:** `text-embedding-3-small` (1536-d).
- **Cost:** signature **per-turn tax** — system prompt + all core blocks + full tool JSON schemas re-sent every turn (~3,444 tok in our prior study); retrieval **requires ≥1 LLM call** (agent decides to search).
- **By shape:** thematic **strong** (semantic + datetime/tag filters); pinpoint **weak** on NATIVE cosine; lifecycle **weakest** (single top-k, completeness depends on the agent re-querying).
- **Published:** MemGPT paper DMR **92.5%** acc / ROUGE-L 0.814 (GPT-4) — but on a **synthetic** deep-memory-retrieval set, not curated work cards.

## graphify (`@sentropic/graphify`, npm `graphifyy`)
- **Domain:** a **code** knowledge-graph (tree-sitter AST → typed entity/relation graph). **Retrieval = BFS/shortest-path traversal** over the graph (no vectors), Louvain communities for navigation.
- **Cost:** ~1.7–2K-token subgraph per query + an always-on `GRAPH_REPORT.md` injected before Grep/Glob.
- **Applicability:** **poor fit** for a work-log corpus — its queries are structural ("find callers of X", "path between symbols"), not temporal/semantic ("what shipped on auth in May"). **Excluded as a recall rival; kept only as a token-efficiency calibration peer.**

---

### What this means for WorkBoard's design
The peers win **thematic** (dense semantics) and lose **pinpoint** + **lifecycle** — the two shapes a work ledger leans on. WorkBoard's structured cards make pinpoint a *deterministic resolve* and lifecycle a *graph walk*, with **zero embeddings, zero vector DB, zero per-session/per-turn LLM tax**. So the right matcher to build is a **field-weighted lexical (BM25F) + literal/#ref + link-traversal** ranker — which is exactly what the bake-off measures. See `../REPORT.md`.

*Full per-peer evidence (file paths, line refs, benchmark URLs) captured in the study run; cards #730/#733/#734/#735/#782.*
