# OVERVIEW — WorkBoard vs Graphify, in plain language

A from-scratch explainer of what each system is, how they were compared, and
where WorkBoard is stronger and weaker than Graphify. For the numbers and method
detail see `REPORT.md` / `REPORT_DETAILED.md`; for the full build story see
`CONTEXT.md`.

---

## How Graphify works

Graphify is a **code-structure knowledge graph**. You point it at a repo
(`graphify .`); it uses **tree-sitter** to parse your source into an AST and emits
a `graph.json` of **nodes** (functions, files, classes) and **edges** (calls,
imports, relationships), clustered into communities. On the 37 scripts used here
it built **710 nodes / 1,396 edges**.

- **Writes are local, 0 API tokens** — pure AST parsing, no LLM (docs/PDFs are the
  exception; those get sent to a model with a `--token-budget`).
- **You query it** with `graphify query "what connects auth to the DB?"` → it runs
  a BFS traversal and returns a **matching subgraph** (~1,374 tok mean here), never
  the whole graph.
- **Claude integration** (measured from a real install): a **Skill** + **one line
  in `CLAUDE.md`** + **on-demand reference docs** + an optional **git post-commit
  hook** that rebuilds the graph. Crucially: **no per-prompt hook, no per-tool
  injection.**

One-liner: **Graphify maps your code — "what calls what."**

## How WorkBoard works

WorkBoard is a **work-outcome ledger** — a kanban board where each card carries
title (what) · origin (why) · subtasks (how) · writeup (commits/files/
verification) · history · links.

- **Writes are deterministic, 0 model tokens** — `card.py add/fly` is a CLI; the
  writeup is just the model's normal turn output committed to `board.json`.
- **You recall** with `card.py show/list` (~2,399 tok mean for a work question).
- **Context injection**: a ~97-tok SessionStart digest + a **~306-tok nudge every
  prompt** (the lever that keeps carding live — trimmable to ~40).
- The 130KB+ `board.json` is **never auto-loaded**.

One-liner: **WorkBoard maps your work — "what shipped, why, what's open."**

## How WorkBoard fares against Graphify (honest verdict)

**WorkBoard does *not* beat Graphify on efficiency — and that is the real
finding.** Against claude-mem / mem0 / Letta, WorkBoard wins big because those
tools burn LLM tokens to *write* memory every session or every turn. **Graphify
has no such cost** — its writes are local AST at 0 tokens, exactly like
WorkBoard's `card.py`. So they are **close on live tokens and differ mainly in
what they remember.** Complements, not competitors.

Measured head-to-head (same tokenizer, tiktoken cl100k):

| Axis | WorkBoard | Graphify | Winner |
|---|--:|--:|---|
| Always-on / prompt | 306 | 61 (cached) | **Graphify** |
| SKILL.md on engage | **5,898** | 8,245 (+9,704 refs) | **WorkBoard −28.5%** |
| Per recall | 2,399 (work Qs) | 1,374 (code Qs) | different questions |
| Write / keep current | 0 | 0 | **tie** |
| Big artifact autoload | never | never | **tie** |

## How they were compared

1. **Installed Graphify for real**, sandboxed — throwaway venv + a fake `$HOME` so
   it could not touch the real `~/.claude`.
2. **Built a real graph** on a frozen copy of `WorkBoard/scripts/*.py`, and ran a
   5-query set to measure subgraph payloads.
3. **Tokenized everything with one shared tokenizer** (fairness — neither side
   gets a friendly counter).
4. **Measured WorkBoard the same way** on the same frozen board snapshot
   (SKILL.md, nudge, digest, recall).
5. **Modeled a live session** (per-prompt sweep + component totals) and rendered
   the reports.

The key discipline: when Graphify's rendered docs claimed it "fires a PreToolUse
hook before every file read" (which would have been a huge WorkBoard win), the
**real install disproved it** — no hook at all. We measured rather than assumed,
and that killed the headline we had expected.

## Where WorkBoard is better

- **Lighter SKILL.md** on engagement (−28.5%, and far more once Graphify pulls its
  references).
- **Records work outcomes** Graphify structurally *cannot*: why a change happened,
  what shipped, links between efforts, lifecycle. Ask Graphify "why did we touch
  auth in May?" and it has no answer; ask "what calls `validateToken`?" and
  WorkBoard has no answer.
- **Human-glanceable** kanban UI; deterministic recall (`card.py show 142` is
  always the same).

## Where WorkBoard is weaker

- **Heavier per prompt**: the 306-tok nudge fires every turn; Graphify injects
  **nothing** per prompt. Over a 50-prompt session that single difference is the
  whole steady-state gap (trimming the nudge to ~40 mostly closes it — that's the
  recommendation).
- **Leaner per-recall on Graphify's turf**: Graphify's subgraph (1,374) is cheaper
  than WorkBoard's content-rich cards (2,399) — though they answer different
  questions.
- **No code-structure understanding**: WorkBoard knows nothing about the call
  graph / dependencies; that is exactly Graphify's home.

## Bottom line

Two lightweight, on-disk, query-on-demand systems that remember different layers
of the same project — **Graphify = your code map, WorkBoard = your work map.** The
right move is to use both, not pick one.
