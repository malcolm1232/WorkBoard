# Token-Efficiency Summary — WorkBoard vs mem0 · claude-mem · Letta · graphify

A neutral compilation of the four head-to-head studies in this directory. Every
number is from each study's own auto-generated reports (same tokenizer —
`tiktoken cl100k` — for every system; medium corpus = 933 real Claude-Code
sessions). Peers are measured/modeled from their **own** published numbers, with
settings that **favor the peer**, so WorkBoard's margins are conservative floors.

> Per-study detail: [`mem0-comparison/`](mem0-comparison/) ·
> [`claude-mem/`](claude-mem/) · [`letta-comparison/`](letta-comparison/) (Letta).

---

## The one-liners

- **WorkBoard builds its memory with ~98–99% fewer tokens** than mem0 and claude-mem
  (it filters deterministically before spending any model tokens).
- **WorkBoard persists work for free** — **0 model calls/session** vs an LLM
  extraction/compression call *every session* for mem0 and claude-mem.
- **Over a project's life, WorkBoard runs the memory loop 34–81% cheaper** than the
  three memory systems.
- **Honest:** on a *single* lookup, mem0 and Letta are actually *leaner* than
  WorkBoard; WorkBoard wins the **loop**, not the **lookup**. And graphify is a
  different-domain tool (code graph), included only as a calibration.

---

## Headline scoreboard

| Peer | What it is | WorkBoard's headline advantage | Where the peer wins |
|---|---|---|---|
| **mem0** | vector memory (LLM extracts facts → Qdrant) | **Live loop 33.7% fewer** tokens · **build 98.7% fewer** · persists **free** | leaner per single recall (1,800 vs 2,399); no per-turn tax |
| **claude-mem** | vector memory (Chroma; compresses each session) | **Live loop 52.6% fewer** · **build ~99% fewer** · **recall 25.9% lighter** (wins 16/19) | tight single-fact pinpoints + off-board facts |
| **Letta** (MemGPT) | in-context memory blocks re-sent **every turn** | **Live loop 81.0% fewer** (92.2% trimmed) · **no in-context memory tax** | leaner per single recall (1,064 vs 2,399) |
| **graphify** *(graphifyy 0.8.41, real install)* | **code** knowledge-graph (different domain) | **no 95%-style win** — both write **free**; WorkBoard SKILL.md **28.5% lighter** | lighter per-prompt (0 vs WB's 306 nudge); different shape, not a rival |

---

## Per-dimension matrix

| Dimension | vs **mem0** | vs **claude-mem** | vs **Letta** | vs **graphify** |
|---|---|---|---|---|
| **Build memory** (ingest tokens) | WB **98.7% fewer** (64K vs 5.1M) | WB **~99% fewer** (≈11K vs 5.1M) | n/a (per-turn cost) | n/a (different domain) |
| **Persist / session** (write) | WB **0** vs 1 LLM call (~5.5K tok) | WB **0** vs 1 compression call (~5.5K tok) | WB **0** vs LLM tool-calls + compaction | both **0** model tokens |
| **Live loop** (project lifetime) | WB **33.7% fewer** | WB **52.6% fewer** | WB **81.0% fewer** (92.2% trimmed) | — |
| **Per single recall** | mem0 leaner (1,800 vs **2,399**) | **WB 25.9% lighter** (2,399 vs 3,237) | Letta leaner (1,064 vs **2,399**) | graphify leaner (1,373)* |
| **Recall vs full-context** (26K) | WB **90.8% fewer** · mem0 93.1% | — | — | — |
| **Per-turn injection** | WB 306 (trim 40) · mem0 **0** | WB 306 · claude-mem grows w/ memory | WB **306** vs Letta **3,444** | WB 306 vs graphify **0** |

\* graphify's "recall" is a code-subgraph query — a different domain (code structure,
not work outcomes), so it's a different-shape comparison, not a head-to-head. See
the dedicated `graphify-comparison/` study (real `graphifyy 0.8.41` install).

> **Scenario note:** the *live-loop* % for mem0 & claude-mem is over **100 sessions ×
> 3 recalls** (their cost is per-*session*); Letta's is over **100 sessions × 50
> turns × 3 recalls** (its cost is per-*turn*). Each % is computed on that peer's own
> scenario — they are not a shared absolute base.

---

## How to read this (the recurring gotcha)

The two "recall" rows look contradictory but aren't:
- **Per single recall:** mem0 (1,800) and Letta (1,064) are *smaller* than WorkBoard's
  content-rich cards (2,399) — they win the **lookup**.
- **Live loop:** WorkBoard wins by 34–81% — because **building and persisting memory
  are free for WorkBoard**, and that dwarfs any single-recall difference. WorkBoard
  wins the **loop**.

"X% fewer" always means a **reduction** (a saving), same direction as mem0's marketed
"90% fewer."

---

## Why WorkBoard comes out ahead on the loop

mem0, claude-mem, and Letta all pay an **LLM tax to remember**: mem0/claude-mem run an
extraction/compression call every session; Letta re-sends its memory machinery every
turn. **WorkBoard's writes are free** — the card text is the agent's normal turn
output, committed by a deterministic CLI, and the board is never auto-loaded into
context. That structural difference is the whole story.

## What the peers do better (neutral)

- **Zero discipline:** mem0, claude-mem, and Letta capture memory **automatically**,
  across projects, with no habit required. WorkBoard needs the live-carding discipline
  and is project-scoped.
- **Leaner single lookups:** mem0 and Letta inject a smaller bundle per query.
- **Vague semantic recall:** the vector systems can surface things that were never
  explicitly recorded; WorkBoard only knows what was carded.

**Bottom line:** these are complements. The memory systems are *automatic
conversational/semantic memory*; WorkBoard is the *structured, free-to-maintain
project ledger* — cheapest to build, cheapest to keep current, and strongest on
multi-card "what shipped / what's still open" recall.

---

*Sources: `mem0-comparison/REPORT.md`, `claude-mem/REPORT.md`, `letta-comparison/REPORT.md`
(Study 1b, Letta), and `graphify-comparison/REPORT.md` (graphify, real install). Cards
#730 / #733 / #734 / #735 / #749 / #751.*
