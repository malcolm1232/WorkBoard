# WorkBoard vs graphify — live operating cost (measured)

> graphifyy 0.8.41 · tokenizer `tiktoken-cl100k_base` (identical for both = fairness) · board snapshot `7c49f1314c6b87d4`

**Headline.** graphify is a lightweight peer. No 95%-style efficiency win exists vs graphify (unlike vs claude-mem/mem0/Letta, which pay a per-session or per-turn LLM cost). Difference is shape — work outcomes vs code structure — at similar low cost. WorkBoard's one heavier surface is the per-prompt nudge (306 tok, trimmable to 40); graphify injects nothing per prompt.

## What graphify actually is (measured, not assumed)

graphify is a **code-structure knowledge graph** (tree-sitter AST → `graph.json`: 710 nodes / 1396 edges on frozen copy of WorkBoard/scripts/*.py (37 files)). Its Claude Code integration, captured from a real sandboxed install, is:

- Skill + 1 CLAUDE.md line + on-demand references + git post-commit rebuild. NO PreToolUse hook, NO settings.json, NO per-tool-use injection.

This contradicts the rendered-docs claim of a *PreToolUse-fires-on-every-read* hook — the installed tool writes **no `settings.json` and no hook entry**. graphify's per-prompt steady-state cost is **0**; its weight sits in the on-engagement SKILL load and the per-query subgraph.

## Measured live-context footprint (tiktoken cl100k)

| Surface | WorkBoard | graphify | Lighter |
|---|--:|--:|---|
| Always-on / **prompt** | 306 nudge (+97 digest/sess) | 61 once, cached | **graphify** |
| SKILL.md (on engage) | **5898** | 8245 (+9704 refs on demand) | **WorkBoard** (28.5%) |
| Per recall | 2399 (work Qs) | 1374 (code Qs) | different questions* |
| Write / keep current | 0 (card.py) | 0 (local AST) | **tie** |
| Big artifact autoload | board.json never | graph.json never | **tie** |

\* WorkBoard recall answers *what shipped / why / links*; graphify recall answers *what calls what*. Token counts price different questions — not a head-to-head.

## The per-prompt nudge — WorkBoard's one heavier surface

WorkBoard injects a protocol nudge **every prompt**; that lever makes carding a **0-model-token deterministic write**. graphify injects nothing per prompt. Over a session this is the whole steady-state gap:

| Prompts | graphify | WorkBoard (306) | WorkBoard trimmed (40) |
|--:|--:|--:|--:|
| 1 | 0 | 306 | 40 |
| 10 | 0 | 3060 | 400 |
| 50 | 0 | 15300 | 2000 |
| 100 | 0 | 30600 | 4000 |

Trimming the nudge to ~40 tok (already a `TOKEN_BUDGET.md` lever) closes most of the gap. **Recommendation: trim the nudge.** This study makes no product change.

## Illustrative session total (50 prompts, 1 skill load, 3 recalls)

| | graphify | WorkBoard | WorkBoard trimmed |
|---|--:|--:|--:|
| always-on | 61 | 97 | 97 |
| per-prompt | 0 | 15300 | 2000 |
| SKILL load | 8245 | 5898 | 5898 |
| recall* | 4122 | 7197 | 7197 |
| write | 0 | 0 | 0 |
| **total** | 12428 | 28492 | 15192 |

\* recall mixes question types; shown for completeness. Honest read: with the full nudge WorkBoard is heavier in steady state; trimmed it is comparable; graphify is genuinely light.

## Bootstrap (kept light, honest)

graphify code ingest is local tree-sitter AST — ~0 API tokens. Only docs/PDF ingest spends model tokens (configurable --token-budget). So bootstrap is NOT a WorkBoard win for code; stated honestly.

## Conclusion

Against **claude-mem / mem0 / Letta**, WorkBoard wins the live loop because those tools pay an LLM cost per session (claude-mem/mem0) or per turn (Letta). Against **graphify** that lever does not exist — graphify's writes are local AST at 0 API tokens too. Honest verdict: **different shape, similar low cost.** WorkBoard records *work outcomes*; graphify records *code structure*. Complements, not competitors.

> Reproduce: `python3 run_live_graphify.py && python3 render_report.py`. Re-measure graphify from scratch: see `measure_graphify_real.md`. Full story: `CONTEXT.md`.
