# Haiku-fill performance — findings & levers

How `--bootstrap-mode haiku` / `--fill haiku` wall time was reduced, and what each
knob actually does. Measured 2026-05-31 (`--days 5`, 73 chunks of dense history).

## Mental model

The haiku fill does three things:
1. **Chunk** the history into time-buckets, bundled into "chunks".
2. **Extract** — spawn one `claude -p --model haiku` per chunk, several at once,
   each returning a JSON array of cards.
3. **Emit** — add each card to the board, one by one.

So roughly:

> **wall ≈ (number of chunks ÷ workers) × per-call-time  +  per-card emit delay × number of cards**

Once `MAX_THINKING_TOKENS=0` removed the per-call *generation* cost (the original
6× fix), the remaining time is dominated by **how many calls run at once
(workers)**, **the fixed startup cost per call**, and a **hidden serial emit
floor (pace)**.

## The levers (plain English)

### 1. `workers` — how many `claude -p` calls run AT THE SAME TIME  ✅ biggest lever
Like checkout lanes: 4 lanes vs 8 lanes. 8 chunks get processed simultaneously
instead of 4. **Measured: 4→8 = 2.0× faster, 0 failures.** 8→12 helps less
(diminishing). New default: **8**. (The old "don't raise workers" advice was from
the thinking-on era, when calls were slow for a different reason — it's now wrong.)

### 2. `--strict-mcp-config` — skip loading MCP tools on each call  ✅ free win
Every time `claude -p` starts, it connects your MCP integrations (Gmail, Drive,
Calendar, QuantifyMe…). That handshake costs ~2s **per call**, and card extraction
never uses those tools. Stripping them saves ~2s on every call ("hi" test:
4.8s → 2.9s). Small alone, but × hundreds of calls it adds up. Lives in
`_LLM_ARGS` (hourly_common) so both call sites share it.

### 3. `pace` — the GAP between cards appearing  ✅ hidden serial floor
`pace` is a backend `sleep(pace_s)` *between emitting one card and the next* — so
cards stream in one-at-a-time instead of all at once. **It is NOT the flight
animation** (that's `--show-lifecycle`, below). At `pace=0.3`, the backend waits
0.3s after each card. For a few cards that's a pleasant one-by-one feel; for 453
cards it's **0.3 × 453 ≈ 136s of pure waiting**. `--days 5`: pace 0.3 → 229s,
pace 0.05 → 142s. Fix: the **bulk backfill (tier-2) runs `--pace 0.02`**; the
**watched recent day (tier-1) keeps the deliberate one-by-one pace** + flights.

### 4. `--show-lifecycle` — the flight animation  ✅ tier-1 only
Makes a done-card visibly fly task → In Progress → Done (3 hops) instead of just
appearing in Done. Nice theatre, but costs time per card. Kept on the **watched
tier-1**; dropped on the bulk backfill (cards just appear).

### 5. `chunk-size` — buckets bundled per call  ❌ NOT a lever (disproven twice)
We expected bigger bundles = fewer calls = faster. It backfired: bigger chunks
make each call generate more (slower), **under-utilize the workers** (fewer chunks
than lanes = idle lanes), AND **merge separate tasks into fewer cards** (89 → 56
on the same buckets — a quality loss). Kept small (**2**).

## Result

| Config | `--days 5` wall |
|---|---|
| original (workers 4, pace 0.3, no MCP-strip) | ~337s |
| **optimized (workers 8 + `--strict-mcp-config` + tier-2 pace 0.02)** | **142s (~2.4×)** |

0 timeouts, cards intact. Same factor extrapolates to 60-day windows.

## The remaining floor

After these, wall is **generation-bound**: total card content ÷ workers. Only two
things beat it further, both with costs you've weighed:
- **more workers** (12 helps a bit more, diminishing; heavier on small machines),
- **less content** (no-jsonl ≈ ½ the cards, but loses the open-item/intent richness).

Shipped: `9f3cfee` (workers/MCP/pace/lifecycle), on top of `f36a25b` (thinking-off)
and `5327920` (JSON salvage). See card #326 and `MEMORY` /
`project_haiku_emit_serial_bottleneck`.
