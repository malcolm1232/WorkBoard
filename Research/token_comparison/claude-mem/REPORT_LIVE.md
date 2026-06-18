# Study C — Live carding: cost to RUN with the memory on

> Auto-generated from `results/raw/live.json`. Tokenizer `tiktoken-cl100k_base` (identical for both systems). Card #730. Part of the WorkBoard vs claude-mem study (`REPORT.md`).

## The question

Bootstrap (Study A) is the one-time build. **Live carding** is the steady-state cost: while you actually work, what does each system add — in model calls, model tokens, and injected context — to keep memory current and answer questions? This is the cost you pay *every session, forever*.

## Three cost dimensions (measured the same way for both)

### (1) Memory-write — model cost to persist the session's work

| | model calls / session | model input tok / session | over 100 sessions |
|---|--:|--:|--:|
| **WorkBoard** — inline carding | 0 | 0 | 0 |
| **claude-mem** — SessionEnd compress | 1 | 5,462 | 546,200 |

**Derivation.** WorkBoard's carding is a deterministic `card.py add/fly` call; the card's writeup is text the main model already produced during the turn, so persisting it costs **no extra model call**. claude-mem's SessionEnd hook runs **one Agent-SDK compression call per session** over the full transcript — on the medium corpus that averages **5,462 input tok/session** (5,095,769 ÷ 933 sessions). Over a project's life this is the bootstrap cost paid again every session.

### (2) Context-injection — interactive tokens added per session

| | SessionStart | Per turn | 50-turn session | Scales w/ memory size? |
|---|--:|--:|--:|:--:|
| **WorkBoard** (full nudge) | 97 | 306 | 15,397 | **No** (board never auto-loads) |
| **WorkBoard** (trimmed nudge) | 97 | 40 | 2,097 | **No** |
| **claude-mem** | ~thousands (grows with stored memory) | — | — | **Yes** |

WorkBoard's injection is a fixed protocol digest + a per-turn nudge; the 130KB+ `board.json` is **never auto-loaded** (CLI-only access), so injection is **constant in board size**. claude-mem injects a memory block at SessionStart that grows as stored memory grows (docs/TOKEN_BUDGET.md).

### (3) Per-recall — tokens to answer one question (from Study B)

| System | Tokens / recall | vs WorkBoard |
|---|--:|--:|
| **WorkBoard** (measured) | 2,399 | — |
| claude-mem (spec best-case) | 3,237 | WB 25.9% lighter |
| mem0 (cited) | 6,719 | WB 64.3% lighter |

## The nudge tradeoff — breakeven analysis

WorkBoard's per-turn nudge is its **one heavier surface** — and it's the lever that makes memory-write free. An honest question: at what session length does WorkBoard's cumulative nudge overhead equal claude-mem's single per-session compression call?

- Full nudge (306/turn): breakeven at **~18 turns** (5,462 ÷ 306).
- Trimmed nudge (40/turn): breakeven at **~137 turns** (5,462 ÷ 40).

So for sessions shorter than the breakeven, WorkBoard is lighter on this axis too; for longer sessions its nudge overhead exceeds claude-mem's compression. **Important caveat:** these token *types differ* — WorkBoard's nudge is interactive in-context tokens, while claude-mem's compression is a **separate call**. A real sandboxed run (REAL_RUN_FINDINGS.md) confirmed that call runs on the **main Claude model via subscription** (the Claude Agent SDK), *not* a cheaper tier — so it is not discountable. The clean, rate-independent win is dimension (1): WorkBoard adds **zero model calls** to persist work; claude-mem adds one full-tier call every session. Trim the nudge and WorkBoard leads every axis.

## Putting it together — a 100-session project

- **Memory-write model tokens:** WorkBoard **0** vs claude-mem **546,200** (100 compression calls).
- **Per recall:** WorkBoard 2,399 vs claude-mem 3,237 (25.9% lighter).
- **SessionStart:** WorkBoard constant (97 tok, board never loaded); claude-mem grows with memory.

## Honest caveats

- The **per-turn nudge** (306 tok) is real interactive overhead and WorkBoard's least-flattering surface; it is trimmable to ~40 (TOKEN_BUDGET.md) but ships at 306 today.
- claude-mem's per-turn hook injection is **configurable** and not modeled here (we don't fabricate it); the comparison centers on the per-session write, which is its documented mechanism.
- claude-mem's compression buys richer conversational recall; WorkBoard's free writes capture **work outcomes**, not the whole conversation. Different value, honestly different cost.

## Reproduce

```bash
cd ~/Desktop/claude-mem-comparison
python3 run_bootstrap.py      # provides per-session transcript avg
python3 run_recall.py         # per-recall numbers
python3 replay_session.py     # writes results/raw/live.json
python3 report_live.py        # regenerate this file
```

## Raw data

```json
{
  "tokenizer": "tiktoken-cl100k_base",
  "scenario": {
    "turns": 50
  },
  "memory_write_per_session": {
    "workboard_model_calls": 0,
    "workboard_model_tokens": 0,
    "claude_mem_model_calls": 1,
    "claude_mem_model_input_tokens": 5462,
    "note": "WorkBoard cards inline (deterministic CLI); claude-mem runs 1 SessionEnd compression call/session reading the full transcript."
  },
  "memory_write_projection": {
    "sessions": 100,
    "workboard_write_calls": 0,
    "workboard_write_tokens": 0,
    "claude_mem_write_calls": 100,
    "claude_mem_write_tokens": 546200
  },
  "context_injection_per_session": {
    "workboard_sessionstart": 97,
    "workboard_per_turn_nudge": 306,
    "workboard_per_turn_nudge_trimmed": 40,
    "workboard_session_inject_full": 15397,
    "workboard_session_inject_trimmed": 2097,
    "workboard_board_autoload": 0,
    "workboard_scales_with_memory_size": false,
    "claude_mem_sessionstart": "~thousands (grows with stored memory)",
    "claude_mem_scales_with_memory_size": true
  },
  "per_recall": {
    "workboard_measured": 2399,
    "claude_mem_spec_bestcase": 3237,
    "mem0_cited": 6719,
    "wb_vs_cm_pct": 25.9,
    "wb_vs_mem0_pct": 64.3
  }
}
```
