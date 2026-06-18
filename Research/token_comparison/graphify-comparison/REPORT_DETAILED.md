# WorkBoard vs graphify — DETAILED report

> graphifyy 0.8.41 · measured 2026-06-18 · tokenizer `tiktoken-cl100k_base` (one tokenizer for both = fairness) · board snapshot `7c49f1314c6b87d4` (1155340 B)

## 0. TL;DR

graphify is a lightweight peer. No 95%-style efficiency win exists vs graphify (unlike vs claude-mem/mem0/Letta, which pay a per-session or per-turn LLM cost). Difference is shape — work outcomes vs code structure — at similar low cost. WorkBoard's one heavier surface is the per-prompt nudge (306 tok, trimmable to 40); graphify injects nothing per prompt.

There is **no defensible 95%-style efficiency headline vs graphify** — and that is itself the finding. claude-mem/mem0/Letta lose the live loop because they spend LLM tokens to *write* memory (per session or per turn). graphify does not: its graph is built by local tree-sitter AST at **0 API tokens**, the same order as WorkBoard's deterministic `card.py` write. The two systems are close on raw live tokens and differ mainly in **what they remember**.

## 1. Why graphify is a different shape than the other peers

| | claude-mem / mem0 / Letta | graphify | WorkBoard |
|---|---|---|---|
| Stores | conversation memory (vectors) | **code structure** (AST graph) | **work outcomes** (kanban cards) |
| Write path | LLM extraction/compression | local tree-sitter AST | deterministic `card.py` |
| Write token cost | thousands/session or /turn | **0 API** | **0 model** |
| Answers | 'what did we discuss' | 'what calls what' | 'what shipped / why / links' |

Because graphify and WorkBoard both move the write off the model, the live-loop argument that beat claude-mem/mem0/Letta **does not apply to graphify**. The honest comparison is the *context footprint* of having each on, plus a frank statement that they answer different questions.

## 2. What graphify installs (measured from a real sandboxed run)

`graphifyy 0.8.41` `graphify install --platform claude`, run under a throwaway `$HOME`, writes exactly:

```
$HOME/.claude/CLAUDE.md                     <- 1 trigger line (always-on, cached)
$HOME/.claude/skills/graphify/SKILL.md      <- loaded when skill engages
$HOME/.claude/skills/graphify/references/*  <- loaded ON DEMAND only
(no settings.json, no hooks entry)          <- => 0 per-prompt injection
```
Plus an **optional** `graphify hook install` = a *git post-commit* hook that rebuilds the graph locally on commit (tree-sitter AST, no LLM). So there is **no per-tool-use PreToolUse hook** — the widely-quoted 'fires before every file read' description does not match the installed artifact.

## 3. Every measured number (tiktoken cl100k)

### graphify

| Constant | Tokens | Loaded |
|---|--:|---|
| CLAUDE.md trigger line | 61 | always-on (cached once/session) |
| SKILL.md | 8245 | on engagement |
| references/*.md (total) | 9704 | on demand only |
| query subgraph (mean of 5) | 1374 | per `graphify query` |
| &nbsp;&nbsp;query samples | 1807, 1101, 1742, 553, 1669 | (BFS depth-2, code Qs) |
| write (code) | 0 | local AST, no LLM |
| per-prompt injection | 0 | none installed |

Graph built on frozen copy of WorkBoard/scripts/*.py (37 files): **710 nodes / 1396 edges**, `graph.json` = 645,303 B (never auto-loaded; queries pull a subgraph).

### WorkBoard

| Constant | Tokens | Loaded |
|---|--:|---|
| SessionStart digest | 97 | once/session |
| UserPromptSubmit nudge | 306 | **every prompt** |
| &nbsp;&nbsp;nudge trimmed | 40 | trim lever (TOKEN_BUDGET.md) |
| SKILL.md | 5898 | on engagement |
| recall (mean) | 2399 | per work query — reused from claude-mem study (same frozen board_snapshot.json), work-outcome queries n=19 |
| write | 0 | deterministic `card.py` |

## 4. Component-by-component verdict

| Axis | WorkBoard | graphify | Winner & why |
|---|--:|--:|---|
| Always-on / prompt | 306 | 61 (cached) | **graphify** — it injects nothing per prompt |
| SKILL.md on engage | 5898 | 8245 | **WorkBoard** (28.5% lighter; graphify can pull +9704 refs) |
| Per recall | 2399 | 1374 | different questions — not a head-to-head |
| Write / keep current | 0 | 0 | **tie** — different artifacts |
| Big artifact autoload | 0 | 0 | **tie** — both on disk |

## 5. The per-prompt nudge is the whole steady-state gap

graphify adds 0 tokens per prompt; WorkBoard adds the nudge. Across a session that single difference dominates everything else:

| Prompts (T) | graphify | WorkBoard (306) | WorkBoard trimmed (40) |
|--:|--:|--:|--:|
| 1 | 0 | 306 | 40 |
| 10 | 0 | 3060 | 400 |
| 50 | 0 | 15300 | 2000 |
| 100 | 0 | 30600 | 4000 |

The nudge is **not free overhead** — it is the mechanism that makes WorkBoard's write cost 0 and keeps the board live. But it is trimmable to ~40 tok with no loss of the carding contract. **Recommendation: trim it** (turns a per-prompt liability into a rounding error).

## 6. Illustrative full session (50 prompts, 1 skill load, 3 recalls)

| Component | graphify | WorkBoard | WorkBoard trimmed |
|---|--:|--:|--:|
| always-on | 61 | 97 | 97 |
| per-prompt | 0 | 15300 | 2000 |
| SKILL load | 8245 | 5898 | 5898 |
| recall (diff. Qs) | 4122 | 7197 | 7197 |
| write | 0 | 0 | 0 |
| **TOTAL** | 12428 | 28492 | 15192 |

Read it honestly: at full nudge WorkBoard's session total is **higher** than graphify's; trimmed, they are comparable. Neither is dramatically cheaper. The recall row mixes question types and should not be read as a head-to-head.

## 7. Bootstrap (intentionally light)

graphify code ingest is local tree-sitter AST — ~0 API tokens. Only docs/PDF ingest spends model tokens (configurable --token-budget). So bootstrap is NOT a WorkBoard win for code; stated honestly. WorkBoard's bootstrap (mining past sessions into cards) is a one-time Haiku cost documented in the claude-mem study; it is not re-litigated here because graphify's code ingest is a different operation (parsing source, not summarizing history).

## 8. Fairness controls

- **One tokenizer** (tiktoken cl100k) for every count, both systems.

- **graphify measured, not invented** — real install, real graph, real queries; where a number could go either way it was taken in graphify's favor (e.g. references counted as on-demand, not always-on).

- **Shape difference stated up front** — no token 'win' is claimed on questions graphify cannot answer, and graphify's leaner per-recall is reported plainly.

- **Product untouched** — graphify ran under a sandbox `$HOME`; the harness only reads frozen copies and writes inside this folder (`lib/safety.py` enforces it).

## 9. Conclusion

graphify is the one peer in this suite that is **not** beaten on live tokens, because it shares WorkBoard's core efficiency move (keep the big artifact on disk; write without the model). The right public statement is **not** 'WorkBoard is X% cheaper than graphify' — it is *'WorkBoard and graphify are both lightweight, on-disk, query-on-demand systems that remember different things: graphify maps your code, WorkBoard maps your work.'* Save the efficiency headlines for claude-mem / mem0 / Letta, where the per-session/per-turn LLM tax makes them real.

> Reproduce: `python3 run_live_graphify.py && python3 render_report.py`. Re-measure: `measure_graphify_real.md`. Full provenance: `CONTEXT.md`.
