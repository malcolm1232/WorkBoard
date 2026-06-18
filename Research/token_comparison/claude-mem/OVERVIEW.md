# Overview — WorkBoard vs claude-mem (read this first)

Plain-English walkthrough of how the two systems work, how they were compared, and
where WorkBoard (WB) is better and weaker than claude-mem. For the hard numbers see
`REPORT.md` (concise) or `REPORT_FULL.md` (exhaustive); a map of every doc is at the
bottom.

> All numbers below are from the study's own auto-generated reports (medium corpus:
> 933 real Claude-Code sessions, 2026-05-28→06-10). Same tokenizer for both systems.

---

## How claude-mem works

A **vector-memory layer** that runs as a background service (worker on :37777,
Bun + uv + a Chroma vector DB + SQLite/FTS5).

- **Write (per session):** a `SessionEnd` hook fires one **LLM "compression" call**
  that reads the *whole transcript*, extracts **"observations"** + a summary, and
  stores them as **embeddings** in Chroma. Fully automatic, cross-project, zero
  discipline required.
- **Read (per query):** a **3-layer semantic search** — `search` (compact index,
  ~50–100 tok/result) → optional `timeline` → `get_observations` (full detail,
  ~500–1,000 tok/result). Retrieval is **probabilistic similarity**, not a
  structured lookup.
- **Its marketing claim** ("~95% / ~10× savings") is measured **vs naively reloading
  the full transcript** — not vs another memory system.

## How WorkBoard works

A **structured knowledge-graph of work** — a kanban `board.json` of cards, each with
`title · origin (why) · subtasks · writeup (what shipped + commits/files) · history
· links`.

- **Write (per session):** **inline carding** — the agent that's *already in the
  loop* emits `card.py add/fly` as part of its normal turn. The writeup is text the
  model produced anyway → committed by a deterministic CLI. **0 dedicated LLM calls.**
- **Read (per query):** **progressive disclosure** — `card.py list` (a grep index,
  ~38 tok/line) → `card.py show <n>` (one compact card). Deterministic graph
  traversal, not vector similarity. The 130 KB+ `board.json` is **never auto-loaded**.
- **Bootstrap:** deterministic hourly harvest+bucketize of past transcripts → compact
  digests → one cheap Haiku call *per hour-bucket* (not per session).

## How they were compared (method)

Same corpus, same ruler, head-to-head:

- **Same frozen corpus** of real transcripts (fingerprinted; excludes the Jun 11–15
  inactivity gap).
- **Same tokenizer** (tiktoken cl100k) for both — the single most important fairness
  control.
- **WB = real, measured** (actual bootstrap path in a sandboxed `$HOME`; real
  `card.py` recall against a frozen board snapshot — never the live board).
- **claude-mem = its OWN published per-layer numbers**, set to its mid/best case
  (`fragmentation=1.0` even *gives* it WB's consolidation benefit) — so it can't be
  accused of sandbagging. A **real sandboxed run** (`REAL_RUN_FINDINGS.md`) confirmed
  the structure: 1 compression call/session over the full transcript, no bulk mode.
- **20 gold queries** (pinpoint / thematic / lifecycle), answers written *before*
  querying. **Correctness is real** — a WB answer only counts if every gold fact
  *literally appears* in a fetched card.

## The scoreboard (medium corpus, 933 sessions)

| Dimension | WorkBoard | claude-mem | WB result |
|---|--:|--:|---|
| **Build memory** (input tokens) | 10,546 | 5,095,769 | **99.8% fewer** (~10× fewer calls) |
| **Recall** (mean tok/answer) | 2,399 | 3,237 | **25.9% fewer**, wins 16/19 |
| **Recall — lifecycle** | 2,864 | 4,250 | **32.6% fewer** |
| **Persist as you work** (/100 sessions) | 0 | 546,200 | **free vs an LLM call every session** |

## Where WorkBoard is **better**

- **Multi-fact / lifecycle questions** ("what shipped *and* what's still open, and
  why") — it walks the graph (origin → subtasks → writeup → links) instead of
  re-reading fragmented observations. Biggest margin (**32.6%**).
- **Near-zero build and steady-state cost** — writes are free; claude-mem pays a
  compression call on *every* session, forever.
- **Constant in memory size** — `board.json` is never auto-loaded, so injection
  doesn't grow as memory grows (claude-mem's SessionStart injection does).
- **Deterministic & reproducible** — same query → same cards → same tokens, every
  time. Plus it's a **human-glanceable kanban**.

## Where WorkBoard is **weaker** (honest)

- **Off-board / vague semantic recall.** Anything never carded (operational trivia —
  e.g. query **P06**, a backup-dir name) the board simply doesn't hold; claude-mem's
  vector store can surface it from raw transcripts. **WB only knows what was carded.**
- **Tight single-fact pinpoints.** When the answer is one short fact, a compressed
  observation can be *smaller* than a content-rich card — claude-mem won **P01, P05**
  (3 of 19).
- **Cross-project + zero-discipline capture.** claude-mem auto-captures everything
  across all projects; WB is project-scoped and needs the live-carding habit.
- **The per-turn nudge** (306 tok/turn) is WB's one *heavier* interactive surface —
  though it's trimmable to ~40 and it's the lever that makes writes free.

## Bottom line

They're **complements, not substitutes**: claude-mem is *conversational/semantic
memory* (recall anything you vaguely discussed, across projects, for free effort);
WorkBoard is the *structured project ledger* (what shipped, what's open, the story
behind it — cheap to build, cheap to query, deterministic). WB wins the
**structured-recall + build/steady-state cost** game decisively; claude-mem wins
**unstructured, off-board, cross-project** recall. The honest framing: claude-mem's
"95%" is vs a naive baseline — WB beats that head-to-head on building and using
memory, while conceding the lookups that were never carded.

---

## Which document is which? (doc map)

| File | What it is | Read it when… |
|---|---|---|
| **`OVERVIEW.md`** (this) | Plain-English walkthrough + scoreboard | …you want to *understand* the comparison fast |
| **`README.md`** | Folder/harness usage — what's here, how to run it | …you want to *run* or navigate the study |
| **`REPORT.md`** | Concise auto-generated study report (TL;DR + 3 studies, headline numbers) | …you want the **headline report** |
| **`REPORT_FULL.md`** | Exhaustive auto-generated report (every table + detail) | …you want **everything** |
| **`REPORT_BOOTSTRAP.md`** | Study A only — cost to BUILD the memory | …you care about *bootstrap/ingest* specifically |
| **`REPORT_LIVE.md`** | Study C only — cost to RUN with memory on | …you care about *live/steady-state* specifically |
| **`PROCESS_LOG.md`** | Step-by-step of what was done (audit trail) | …you want to know *how it was built* |
| **`REPRODUCIBILITY.md`** | Provenance + how to re-derive every number | …you want to *verify / reproduce* |
| **`REAL_RUN_FINDINGS.md`** | Findings from the real sandboxed claude-mem run | …you want the *real-run validation* |
| **`run_claude_mem_tiny.md`** | Instructions to run real claude-mem yourself | …you want to *re-run the peer* |

**TL;DR on reports:** **`REPORT.md`** is *the* report (headline). **`REPORT_FULL.md`**
is the same study with every detail. **`README.md`** is just how-to-use-the-folder.
**`OVERVIEW.md`** (this file) is the friendly explainer.
