# Overview — WorkBoard vs mem0 (read this first)

Plain-English walkthrough of how **mem0** works, how **WorkBoard** works, how they
were compared, and where WorkBoard is better and weaker than mem0. For the hard
numbers see `REPORT.md` (headline) or `REPORT_DETAILED.md` (exhaustive). A full doc
map is in `README.md`.

> **This folder studies mem0 only** (WorkBoard vs mem0). The claude-mem and Letta
> comparisons live in their own sibling folders under `../`.
> All numbers below are from this study's auto-generated reports (medium corpus:
> 933 real sessions). Same tokenizer for both systems.

---

## How mem0 works

An open-source **memory layer** (`github.com/mem0ai/mem0`) you attach to an agent.
Needs an **LLM** (OpenAI by default) + a **vector store** (Qdrant self-hosted, or
Mem0 Cloud).

- **Write (per add):** "**single-pass ADD extraction**" — one **LLM call** reads the
  conversation, extracts salient **memory facts**, and stores them as **embeddings**
  (+ optional keyword/entity links). Automatic, cross-project, no discipline needed.
- **Read (per query):** a **vector search** returns a small **flat top-k bundle** of
  relevant facts (~**1.8K tokens** injected). **Nothing is injected per turn** — only
  when you query.
- **Its claim:** "**90% fewer tokens, 91% lower p95 latency, +26% accuracy**" —
  measured **vs full-context** (pasting the whole history, ~26K tok) and vs OpenAI's
  memory, on the LOCOMO benchmark. A vs-naive-baseline number, not a head-to-head
  against a structured peer.

## How WorkBoard works

A **structured knowledge-graph of work** — a kanban of cards (`title · origin ·
subtasks · writeup · history · links`).

- **Write:** **inline carding** — the agent already in the loop emits `card.py` as
  part of its normal turn. **0 dedicated LLM calls.**
- **Read:** deterministic **progressive disclosure** (`card.py list` grep →
  `card.py show`). The 130 KB+ `board.json` is **never auto-loaded**.
- **But:** it pays a **306 tok/turn protocol nudge** every turn (trimmable to ~40).

## How they were compared (method)

- **Same corpus, same tokenizer** (tiktoken cl100k) — the fairness control.
- **WorkBoard = real, measured** (real `card.py` recall against a frozen board
  snapshot; real bootstrap path).
- **mem0 = its OWN published numbers** (flat 1.8K/recall, 1 ADD call/session) — best
  case for mem0, so it can't be accused of sandbagging. (mem0 needs an OpenAI key +
  Qdrant to run, so it's modeled from its paper/blog rather than executed.)
- Same **20 gold queries**; **WorkBoard correctness is real** (every gold fact must
  literally appear in a fetched card).

## The scoreboard (medium corpus, 933 sessions · 100 sessions × 3 recalls)

| Dimension | WorkBoard | mem0 | Result |
|---|--:|--:|---|
| **Build memory** (input tokens) | 64,162 | 5,095,769 | **WB 98.7% fewer** |
| **Persist as you work** (/session) | 0 | 1 LLM call (~5,462 tok) +1 embed | **WB free** |
| **Live loop** (/100 sessions) | 719,700 | 1,086,200 | **WB 33.7% fewer** |
| **Per single recall** | 2,399 | 1,800 | **mem0 leaner** |
| **Recall savings vs full-context** (26K) | 90.8% fewer | 93.1% fewer | **WB ≈ matches mem0's "90%"** |

### How to read each dimension

- **Build memory** = the one-time cost to turn your past history *into* memory.
  WorkBoard does cheap deterministic hourly digests; mem0 sends every session to an
  LLM → WorkBoard reads **98.7% fewer input tokens**.
- **Persist as you work** = the ongoing cost to *save* each session. WorkBoard's
  carding is the agent's normal output (0 extra LLM calls); mem0 spends one LLM
  extraction call (+ an embedding) every session.
- **Live loop** = persist **+** recall combined over 100 sessions — the real
  steady-state cost. **WorkBoard 33.7% fewer.** This is the headline.
- **Per single recall** = tokens to answer **one** question. mem0's flat ~1.8K
  bundle is **smaller** than WorkBoard's content-rich cards (2,399). **mem0 wins
  this one.**
- **Recall savings vs full-context** = how much each saves vs the naive "paste the
  whole 26K history" baseline. **Both are reductions** — WorkBoard saves **90.8%**,
  mem0 saves **93.1%**. mem0's marketed "90% fewer" is exactly this column;
  WorkBoard matches it. (Earlier drafts wrote these as "−90.8%"; the minus just
  meant "below baseline" — it is a *saving*, not a penalty.)

## Where WorkBoard is **better**

- **The live loop** (−33.7% over a project) — because mem0 spends an LLM
  **extraction call on every session** while WorkBoard's writes are free. That write
  tax *is* the margin.
- **Build/bootstrap cost** — 98.7% fewer ingest tokens.
- **Structured, deterministic lifecycle recall** + human-glanceable kanban +
  reproducible answers.

## Where WorkBoard is **weaker** (honest)

- **mem0 wins the single recall.** Its flat ~1.8K bundle is **leaner than
  WorkBoard's content-rich cards** (1,800 vs 2,399). Cheap selective retrieval is
  mem0's whole point, and it holds. **WorkBoard wins the *loop*, not the *lookup*.**
- **mem0 has no per-turn tax.** It injects nothing until you query; WorkBoard pays
  306 tok/turn. So **all-in on long, recall-sparse sessions mem0 can be cheaper**
  unless WorkBoard trims the nudge — breakeven ≈ **12 turns** (full) / ~**89 turns**
  (trimmed) at 3 recalls/session.
- **Cross-project, zero-discipline, automatic capture** + vague semantic recall of
  off-board facts — all mem0; WorkBoard is project-scoped and needs the carding habit.

## Bottom line

mem0 is **genuinely efficient at retrieval** — its "90% vs full-context" is real on
its own terms, and WorkBoard merely *matches* it there (90.8% fewer). WorkBoard
doesn't beat mem0 by recalling smaller; it beats mem0 on the **whole loop**, because
**persistence is free** — mem0 pays an LLM extraction call every single session
forever, WorkBoard pays zero. Complements, not substitutes: mem0 = automatic
cross-project semantic memory; WorkBoard = the structured, free-to-maintain project
ledger.
