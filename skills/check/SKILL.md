---
name: check
description: Search the WorkBoard for past work by natural language — the human-facing front door to `card.py recall`. Use when the user wants to find a card, remember what was done, or check whether something shipped, e.g. "/check the auth redirect bug", "did we ever do X?", "where's the card about Y?", "what shipped on the pulse work?", "find the card for the BM25 matcher". Surfaces the top matching card #s (deterministic, zero-dependency BM25F — no vector DB, no API), then drills in with `card.py show <#>` or walks linked cards with `--traverse`. Triggers: "/check", "check the board for", "did we do/ship/fix", "where's the card about", "find the card for", "search the board", "remind me what we did on".
---

# check — find past work on the board, fast and cheap

The human-facing wrapper over `card.py recall`. Turns a natural-language memory
("the auth redirect thing from last week") into the **top matching card #s**, so
you — or the agent — start from a real, grounded entry point instead of guessing.

Deterministic and **zero-dependency** (stdlib BM25F: no embeddings, no vector DB,
no API key, no model call). It surfaces *entry points* — the detail is pulled on
demand, so a lookup costs a handful of tokens, never a re-read of the whole board.

## When to use
- The user asks "did we ever do / ship / fix X?", "where's the card about Y?",
  "what's the state of the Z work?", "find the card for …", "remind me what we
  did on …".
- The user types `/check <words>`.
- Any time you'd otherwise answer a "what did we do" question from memory — **check
  the board instead** (it's the source of truth; your memory drifts).

## How to run it
The query is the user's own words (strip the leading `/check` if present):

```
python3 <board>/scripts/card.py recall "<the user's words>"
```
where `<board>` is the active board repo (e.g. /Users/malco/Desktop/WorkBoard).

Useful flags:
- `--top N` — how many matches to surface (default 3).
- `--traverse` — also list cards linked to the top matches (walk the graph — for
  multi-card "what shipped + what's still open" lifecycle questions).
- `--json` — machine-readable output.

## Then drill in
- `card.py show <#>` — full detail (origin, notes, writeup, subtasks) for any
  surfaced card.
- Re-run `recall` with different words if nothing strong matches (it stays
  **silent** rather than guessing when there's no real match).

## Present it cleanly
Show the surfaced cards as `#N — title (column)`, then answer the user's actual
question from what `card.py show` returns — cite the card #s and any commit/file
in the writeup. If the question spans several cards (a lifecycle "what shipped +
what's open"), use `--traverse` and summarize across the linked set.

## What this is NOT
- Not a vector/semantic search — it's lexical + structural, so it's strongest on
  exact references (`#627`, a commit sha, a filename) and multi-card lifecycle
  recall, and weaker on open-ended fuzzy-paraphrase topics. For those, surface the
  best entry points and reason over them; don't expect a single perfect hit.
- Not a writer — `check` is **read-only**. It never adds or moves a card.
