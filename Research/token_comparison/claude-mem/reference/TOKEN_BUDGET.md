# Token budget — `board-steward`

Measured 2026-05-28; **per-prompt + install cost re-measured 2026-06-10** (correcting the earlier "no per-prompt injection" claim — the #360 UserPromptSubmit nudge *does* inject every prompt). This doc backs the numbers and sets degradation thresholds.

> Token counts below use the `cl100k` tokenizer as a proxy; Claude's tokenizer runs ~10–15% higher for English+symbols. The install/recon dollar figures are exact (from the `claude` CLI's own `total_cost_usd`).

## TL;DR

- **~222 tokens once/session** for the SessionStart digest.
- **~309 tokens *per prompt*** (≈355 Claude) for the UserPromptSubmit protocol nudge (#360) — injected every turn, NOT zero. Over a 50-turn session that's ~15.7K tokens, the dominant interactive overhead. (Trimmable — see Open hardening work.)
- **~4,065 tokens** when SKILL.md body loads (only on board engagement, not every prompt). *(Was 7,666; the 2026-06-09 core/reference split + de-dup brought it down, and reference moved to `docs/PLAYBOOK.md` loaded on demand. The ≤2,000 cap isn't reachable without cutting rules — the laws alone are ~2.2k — so adherence (laws + worked examples) was prioritized over the number.)*
- **0 tokens** for board.json (~33K on disk, never auto-loaded — CLI-only access).
- **Install/recon backfill: ~23K newly-generated Haiku tokens, one-time** (measured: a default 2-day History-Replay fill = 38 Haiku calls → 81 cards; 200 fresh input + 23K output, the rest is the same context re-read from cache at ~0.1×). Runs as a **detached subprocess on Haiku — the cheapest model tier** — a separate stream that never enters the interactive session's context.
- **Lightest per-prompt skill of the five peers benchmarked.** Heavier on cold-engagement than CLAUDE.md, lighter on every other axis.

## Local measurements

Counted from the live WorkBoard install (rev 1156, 82 cards, 54 in Done).

| Source | Bytes | ≈ Tokens | When loaded |
|---|--:|--:|---|
| SessionStart hook output | 888 | **222** | Once per session |
| **UserPromptSubmit nudge (#360)** | ~1,250 | **309** (≈355 Claude) | **Every prompt** |
| SKILL.md (full) | 30,664 | **7,666** | When skill is engaged (board work) |
| index.json | 39,693 | 9,923 | Only on explicit Read (never auto) |
| board.json | 131,560 | 32,890 | Only on explicit Read (never auto) |
| `card.py list --column X` | 216 | ~54 | Per query |
| `card.py show <num>` | ~4,730 | ~1,180 | Per card detail |
| Done-column writeups (54 cards) | 35,917 | 8,979 | Inside board.json — cold |
| All-card origin text (82) | 16,284 | 4,071 | Inside board.json — cold |
| All-card notes text (82) | 20,364 | 5,091 | Inside board.json — cold |

### Per-session cost model

```
COLD session, 50 turns (no board engagement):
  skill description in skill list    ~80 tok   (always-on, set by harness)
  SessionStart digest                 222 tok  (once)
  UserPromptSubmit nudge #360         309 tok × 50 = 15,450 tok
                                    --------
                                  ~15,750 tok / 50-turn session  (~18.1K Claude)

WARM session (board engaged once, +5 card actions), 50 turns:
  cold baseline (above)            15,750 tok
  SKILL.md body (one load)          7,666 tok
  5 × card.py show                  5,900 tok
                                    --------
                                  ~29,300 tok / session
```

A typical 50-turn coding session at ~2K tokens/turn = ~100K total. Board-steward cold cost ≈ 15.8% of that budget (almost entirely the #360 per-prompt nudge), warm ≈ 29%. **The per-prompt nudge is the lever** — trimming it to a one-liner (~40 tok) drops the cold 50-turn cost to ~2.4K. None of the above includes the install/recon backfill, which is a **separate Haiku token stream** (below) and never touches the interactive context.

## Install / recon backfill — Haiku tokens (measured 2026-06-10)

The History-Replay fill at install (and the SessionStart reconcile sweep) run `claude -p --model haiku` once per history chunk, **detached** — a token stream on **Haiku, the cheapest model tier**, that never enters the interactive session's context. Measured one **default 2-day** bootstrap on WorkBoard's (dense) own history:

| Metric | Value |
|---|--:|
| Haiku calls (22 extraction chunks + recon) | 38 |
| Cards produced | 81 |
| **Newly generated tokens** (fresh input + output) | **~23,449** |
| └ fresh input | 200 |
| └ output | 23,249 |
| Cached context re-read across the 38 calls (billed ~0.1×) | ~1.26M |

**The real cost is the ~23K newly-generated tokens** — and every one is a **Haiku** token, the **cheapest tier Anthropic offers** (per-token, 5× under Opus and 3× under Sonnet). The large cache-read figure is just the same context re-read across calls, billed at a fraction of fresh input — not new consumption. It happens **once at install**, off the interactive path, on the cheapest model, so it never competes with your real session budget. A real user's 2-day window varies with history density; WorkBoard's is on the heavy end (81 cards).

> Not captured automatically yet — the prod path uses `--output-format text` (no `usage`). Measured via a drop-in `claude` wrapper that re-runs with `--output-format json` and tallies `usage`; prod code untouched.

## Peer benchmark

| Skill | Per-prompt | Per-action | 30-day storage | Compaction | Source |
|---|--:|--:|---|---|---|
| **CLAUDE.md baseline** | full file every turn (cached) | 0 | grows unbounded | manual prune | [docs](https://code.claude.com/docs/en/memory) |
| **board-steward** (this) | **~309** (#360 nudge; trimmable to ~40) | 50–1,200 (CLI stdout) | board.json grows ~1–3KB/card; cold on disk | `archive_done.py` >14d | self |
| **claude-mem** | ~thousands at SessionStart | tens-of-K to fetch full | SQLite + ChromaDB, unbounded | AI-compressed summaries | [repo](https://github.com/thedotmack/claude-mem) |
| **graphify** | ~1,500 per PreToolUse fire | ~1,500 per Grep/Glob | pre-compiled graph file | full rebuild | [repo](https://github.com/safishamsi/graphify) |
| **mem0** (MCP) | 0 | 6,719–6,956 per `search_memory` | Qdrant/Valkey vector DB | summarize-on-write | [bench](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm) |
| **letta / MemGPT** (MCP) | ~1,000 baseline (2 core blocks × 500 tok) | tool-call cost for recall/archival | SSD + disk, unbounded | self-evicts core when full | [docs](https://docs.letta.com/guides/core-concepts/memory/memory-blocks/) |

### Where board-steward sits

**Among the lightest per-turn of the set** (~309 tok/prompt, and trimmable to ~40). Closest architectural peer is **graphify** (CLI-as-tool + opt-in injection), but graphify's automatic PreToolUse fire (~1,500/fire) makes its amortized cost higher across a coding session. board-steward also avoids the "grows forever in system prompt" failure mode that bit Claude Code 2.1.96 ([#45188](https://github.com/anthropics/claude-code/issues/45188)) — its 131KB board.json lives on disk, not in context. (mem0 is 0 per-prompt but pays 6.7K per recall; the #360 nudge is the one surface where board-steward could go lower still.)

## Degradation curve

Projected cost at scale (assumes current shape: ~30% Done, ~10% writeup avg 700B):

| Card count | board.json | index.json | SessionStart digest | SKILL.md |
|--:|--:|--:|--:|--:|
| 82 (today) | 131 KB | 40 KB | 222 tok | 7,666 tok |
| 250 | ~400 KB | ~120 KB | ~250 tok | static |
| 500 | ~800 KB | ~240 KB | ~260 tok | static |
| 1,000 | ~1.6 MB | ~480 KB | ~270 tok | static |

board.json and index.json grow linearly, but neither auto-loads, so this doesn't tax context — it taxes disk + the rare explicit `Read board.json` slip.

The SessionStart digest is bounded: counts + last-shipped line are fixed-shape regardless of card count.

## Hard caps + thresholds

Enforced (or to be enforced) so the skill doesn't quietly bloat:

| Surface | Cap | Enforced by |
|---|--:|---|
| SessionStart hook block | ≤ 300 tok | hand-coded; PR-time review |
| SKILL.md (always-load body) | ≤ 2,000 tok (aspirational) | **~4,065** after the 2026-06-09 split + de-dup (#202). Reference → `docs/PLAYBOOK.md`. The ≤2,000 number is sub-rule-set size (laws alone ~2.2k); kept adherence (laws + examples) over the cap. |
| index.json | ≤ 12,000 tok (~48 KB) | `archive_done.py --days 14` sweeps old Done |
| Done writeup median | ≤ 200 tok/card | hand-coded; reconciliation §F flags overruns |
| Single `card.py show` | ≤ 2,000 tok | natural — only bloats if writeup is unbounded |

## Token-cost line for README

> **Cost to install:** ~80 tokens of skill-list description (always-on, set by the harness). ~222 tokens once per session for the board digest, plus ~309 tokens per prompt for the live-protocol nudge. The full SKILL.md (~7.7K tokens) loads only when Claude actively engages with the board. The board itself (board.json, can be 130 KB+) lives on disk and is never auto-loaded — Claude queries it via `card.py` CLI primitives. The one-time History-Replay backfill at install runs on **Haiku — the cheapest model tier** (~23K newly-generated tokens for a 2-day window, measured; the rest is cached context re-read at ~0.1×), as a separate process that consumes **zero** interactive-context tokens.

## Open hardening work (filed as follow-ups)

- ~~**SKILL.md core/reference split**~~ ✅ DONE 2026-06-09 (#202). Split to `docs/PLAYBOOK.md` (full card.py recipes, auto-card markers, tag rules, text-field detail) + `docs/BOOTSTRAP.md` (first-install). De-duped the laws (phase/decompose/under-engage repetition) and ADDED worked `card.py` examples (one per shape) for adherence. 4,754 → ~4,065 tok body. The ≤2,000 target was reframed: the laws are inherently ~2.2k, so adherence was prioritized over the cap.
- **Trim the #360 UserPromptSubmit nudge** — the largest interactive overhead (~309 tok × every prompt). The full lifecycle protocol it repeats each turn is redundant with SessionStart + SKILL.md; a one-liner (`Board live @:<port> — card every ship/fix/defer via card.py; details in the board-steward skill`) cuts it to ~40 tok, reclaiming ~90% of the per-session cost with no rule loss. Highest-leverage token win available.
- **Schedule `archive_done.py`** — Currently a manual script. Wire into a launchd timer (or serve.py background thread) so it runs daily.
- **Token-cost field in telemetry** — Add `est_tokens` to `log_event.py` schema so `report.py` can flag bloat trends over time.
- **Cap enforcement** — `card.py` warns on Done-writeup > 800B; `serve.py` warns on index.json > 50KB.
