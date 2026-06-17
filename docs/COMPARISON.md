# WorkBoard vs claude-mem (and friends)

> A short, honest comparison. The TL;DR: **claude-mem stores memory; WorkBoard is a knowledge graph of work.** They solve different halves of "agent memory" and can comfortably coexist.

---

## The shape difference

| | **claude-mem** (and mem0 / letta) | **WorkBoard** |
|---|---|---|
| **Mental model** | A memory store — past conversation, embedded and recallable | A knowledge graph of work — discrete nodes, explicit edges |
| **What's stored** | Conversation chunks, embedded for semantic search | Cards: `title` · `origin` (why) · `subtasks` (how) · `writeup` (what shipped + commits + files) · `history` (the lifecycle) · `links` (to related cards) |
| **How you retrieve** | Probabilistic similarity search ("find chunks like this query") | Deterministic, structured queries (`show #142`, `query --column done --since may`) |
| **Update model** | Passive — captures as you talk; you query when you remember to | **Hook-enforced live tracking** — the agent literally can't end its turn without carding what shipped (Stop backstop) |
| **Token cost at session start** | Depends on retrieval surface | **~220 tokens** (a tiny digest); the full board (130 KB+) is **never** auto-loaded |
| **Surface for the human** | None — it's a store the agent queries | A live, animated kanban at `127.0.0.1:7891` you actually look at |
| **Scope** | Conversational | Work outcomes (the *signal* — chat noise is dropped) |

---

## Why "knowledge graph" is the better mental model for WorkBoard

A card is a node. Its edges:

- **Subtasks** — the steps Claude actually took (or is taking)
- **Links** — explicit `card.py link <a> <b>` between cards that depend on each other
- **History** — every column move, with `via` (user / agent / undo / declutter), so the lifecycle is replayable
- **Files** — auto-linked via the PreToolUse hook: editing `foo.py` ties the file to the In-Progress card
- **Commits** — writeups carry the commit SHAs that shipped the card

So when a future Claude asks *"why did we touch the auth middleware in May?"*, it doesn't search vectors. It walks the graph:

```
list cards in Done from May with "auth" in title
  → #214 "Rewrite auth middleware"
     ↓ origin (why):   "Legal flagged session-token storage"
     ↓ subtasks (how): split into 4 concrete steps
     ↓ writeup (what): commit 8a748b8, files touched, verification
     ↓ linked cards:   #213 (the legal review), #221 (follow-up bug)
```

That traversal cost a handful of tokens. A vector store doing the same answer would pull conversation chunks, each adding context the model already had.

---

## Where WorkBoard's framing is genuinely more efficient

**Reading the *gist* without re-reading the conversation.** Subtasks compress *how a task was done* into 4–6 lines. The writeup compresses *what shipped*. That's deliberately the "jist" — enough for Claude to pick up exactly where it left off without re-reading whole chat logs or files.

**Determinism.** `card.py show 142` returns the same thing every time. Vector search "tries its best."

**Glanceable to humans.** A kanban is a thing your eyes can scan. A vector store isn't.

**Token-cheap on cold engagement.** WorkBoard injects a ~220-token digest at session start and ~309 tokens per prompt for the lifecycle nudge (measured — see [`TOKEN_BUDGET.md`](TOKEN_BUDGET.md)). Cards are pulled on demand; the 130 KB+ `board.json` never auto-loads. Lightest per-prompt of the five peers benchmarked.

---

## Where claude-mem is genuinely better

**Honest section — not all WorkBoard.**

- **"I vaguely remember we discussed X."** Semantic recall over raw conversation is what embedding stores are built for. WorkBoard only knows what was carded; casual chat that never became a card isn't in the graph.
- **Cross-project memory.** WorkBoard is project-scoped (one board per repo). claude-mem can naturally span everything you've ever discussed with Claude.
- **Zero structure required.** No carding discipline, no hooks — you talk, it captures. WorkBoard requires the agent to keep the board honest (which the hooks enforce, but it's still a contract).
- **Backfilling past context you didn't track.** WorkBoard can *partially* do this via History Replay on install, but a memory store is the native shape for "remember everything from before."

---

## They can (and probably should) coexist

These aren't competitors — they're complements:

- **claude-mem** is your *memory.* Use it for fuzzy recall of past discussions.
- **WorkBoard** is your *project ledger.* Use it for "what work is open, what shipped, what's the story behind it."

A real workflow: **claude-mem remembers the conversation; WorkBoard remembers the work.** When you ask *"what did we ship last sprint?"*, WorkBoard answers in two lines. When you ask *"what was that idea we tossed around about caching?"*, claude-mem is the right tool.

---

## Quick decision matrix

| Question | Prefer |
|---|---|
| "What did we ship to module X in May?" | **WorkBoard** (structured query) |
| "Why did we choose approach Y?" | **WorkBoard** (card's `origin` + `notes`) |
| "Walk me through the lifecycle of feature Z" | **WorkBoard** (card history + subtasks + commits) |
| "I vaguely remember discussing dark mode somewhere…" | **claude-mem** (semantic recall) |
| "What does the agent know about my projects in general?" | **claude-mem** (cross-project memory) |
| "How far through this task are we right now?" | **WorkBoard** (live subtasks: 3/5) |
| "Which file is being worked on?" | **WorkBoard** (auto-linked files on the In-Progress card) |

---

## Caveats worth naming

- This comparison is written by the WorkBoard project. We tried to be fair; if anything here misrepresents claude-mem, please open an issue.
- The token-cost numbers come from real measurements ([`TOKEN_BUDGET.md`](TOKEN_BUDGET.md)) but were taken on a single install; your mileage will vary with board size and engagement.
- Both tools are pre-1.0 and evolving fast. Re-evaluate periodically.
