# Token-Comparison — Summary Findings (cross-peer)

**Umbrella summary across every WorkBoard memory-efficiency study.** Each peer has
its own self-contained study folder; this file is the head-to-head roll-up so the
comparison lives **outside** any single peer folder.

| Peer | Study folder |
|---|---|
| claude-mem | [`claude-mem/`](./claude-mem/) |
| mem0 + Letta (live-loop harness) | [`letta-comparison/`](./letta-comparison/) |

> Numbers are derived from each study's committed reports (`REPORT.md` /
> `REPORT_DETAILED.md`). Same corpus, same tokenizer (`tiktoken cl100k`) for every
> system — the core fairness control. Peers are measured from their OWN published
> numbers (mem0, claude-mem) or their OWN shipped code (Letta) — never our guess of
> their internals.

---

## The headline (all peers, one table)

| Peer | Its marketed claim | Its baseline | WorkBoard head-to-head (live loop) | Per single recall |
|---|---|---|--:|---|
| **mem0** | "90% fewer tokens, 91% lower latency" | full-context (not a peer) | **33.7% fewer** | peer leaner (1,800 vs 2,399) |
| **claude-mem** | "~95% / ~10× savings" | naive full-reload (not a peer) | **52.6% fewer** | **WorkBoard leaner** (2,399 vs 3,237) |
| **Letta (MemGPT)** | per-turn in-context memory | (n/a — measured real) | **81.0% fewer** (92.2% trimmed) | peer leaner (1,064 vs 2,399) |

**One-sentence finding:** every peer markets a big number against a *naive
baseline* (stuffing full context, or naive reload); **none reports a head-to-head
against a structured work-ledger.** Run that head-to-head on real history and
WorkBoard runs the live memory loop with **33.7%–81.0% fewer model tokens** than
the peers — because its writes are free (`card.py`, 0 model tokens) and it carries
no memory in context. It does **not** win every single recall (mem0 and Letta have
leaner per-query retrieval); **it wins the loop, not the lookup.**

---

## Why WorkBoard wins the loop (the structural reason)

| | Memory WRITE (persist a session) | Memory carried in context |
|---|---|---|
| **WorkBoard** | inline `card.py` — **0 model tokens** | **0** (board never auto-loaded) |
| **mem0** | 1 extraction LLM call/session (~5,462 tok) | 0 until you query |
| **claude-mem** | 1 compression LLM call/session (~5,462 tok) | grows at SessionStart |
| **Letta** | an LLM tool-call *per write* + Haiku compaction | **every turn** (~3,444 tok: blocks + tool schemas + system prompt) |

- **mem0 / claude-mem** pay a tax **once per session** (extraction/compression).
- **Letta** pays **every turn** — its memory blocks + memory-tool schemas + MemGPT
  system prompt are re-sent on every interaction. That's why its loop cost is the
  largest of the three, and why WorkBoard's margin vs Letta (81%) is the biggest.
- **WorkBoard** moves the write cost off the model entirely; its only recurring tax
  is a per-turn protocol nudge (306 tok, trimmable to ~40).

---

## Per-peer detail

### vs claude-mem — see [`claude-mem/`](./claude-mem/)
- **Bootstrap (build the memory):** WorkBoard **98.6–99.2% fewer** model-input
  tokens + 5–15× fewer model calls (deterministic harvest/bucketize vs feeding
  whole transcripts to a model).
- **Recall:** WorkBoard **25.9% fewer** tokens (33% on multi-card *lifecycle*
  queries); wins 16/19 answerable queries.
- **Live loop:** WorkBoard **52.6% fewer** model tokens.
- **Honest — claude-mem wins:** tight single-fact *pinpoint* queries and off-board
  facts (things never carded). claude-mem's "95%" is vs naive full-reload, not a peer.

### vs mem0 — see [`letta-comparison/`](./letta-comparison/) (3-way harness)
- **Live loop (100 sessions × 3 recalls):** WorkBoard **33.7% fewer** model tokens
  (719.7K vs 1.086M) — mem0 spends a single-pass ADD extraction call every session.
- **vs full-context (mem0's own baseline):** WorkBoard recall **−90.8%**, matching
  mem0's own "90%" headline on its own terms.
- **Honest — mem0 wins:** its flat ~1,800-tok retrieval bundle is **leaner per
  single recall** than WorkBoard's content-rich cards (2,399). All-in with the full
  306-tok nudge, mem0 wins long/recall-heavy sessions unless the nudge is trimmed
  (full-nudge breakeven ~12 turns; trimmed ~89).

### vs Letta (MemGPT) — see [`letta-comparison/`](./letta-comparison/)
- **Per-turn in-context memory** (measured from Letta 0.16.8's shipped artifacts —
  system prompt 1,061 + tool schemas 2,253 + compiled blocks 130): **3,444 tok/turn**
  (5,289 at full 5000-char block capacity). WorkBoard: 306 (trim 40), 0 carried.
- **Live loop (100 × 50 × 3):** WorkBoard **81.0% fewer** model tokens (92.2%
  trimmed; 87.1% vs Letta's full in-context payload).
- **Live corroboration:** a real local Letta server (Docker `letta/letta` + pgvector,
  Ollama `llama3.2:3b`) replayed real turns → ~10,814 tok/session with real
  memory-tool-calls; real per-turn *exceeds* the structural floor → headline is
  conservative.
- **Honest — Letta wins:** leaner per single recall (~1,064 vs 2,399), and it gives
  autonomous, cross-session, self-editing memory with zero carding discipline.

---

## Method & fairness (applies to all)

1. **Same tokenizer** (`tiktoken cl100k`) counts every token for every system — the
   control that matters most. (It runs ~10–15% under Claude's real tokenizer,
   applied to all → ratios are tokenizer-invariant.)
2. **Same frozen corpus** (real `~/.claude` session history, byte-fingerprinted).
3. **Peers measured by their OWN numbers/code** — can't be accused of sandbagging;
   defaults FAVOR the peers (best-case retrieval).
4. **WorkBoard correctness is real** — a recall counts only if every gold fact
   literally appears in a fetched card.
5. **Non-invasive** — studies read a frozen `board_snapshot.json`; never the live
   board. `lib/safety.py` enforces it.

## The honest one-liner

> On real history, head-to-head, WorkBoard runs the live memory loop with **34%
> fewer tokens than mem0 / 53% than claude-mem / 81% than Letta**, because its
> writes are free and it carries no memory in context. Per *single* recall, mem0
> and Letta are actually leaner — **WorkBoard wins the loop, not the lookup** — and
> its "−90.8% vs full-context" matches mem0's own "90%" headline on mem0's baseline.

---
*Generated 2026-06-18 (rc/s3-letta, card #746). Detail + reproduce steps live in
each study folder's `REPORT_DETAILED.md` + `CONTEXT.md`.*
