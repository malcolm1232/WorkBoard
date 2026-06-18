# Reproducibility & provenance rules

This study compares WorkBoard to claude-mem. **Read this before trusting any
number** — it states exactly what was *measured*, what was *modeled*, and what
was *cited*, so nobody (including a skeptic) has to take a figure on faith.

Card #730 / #736. Tokenizer: `tiktoken cl100k` (same for both systems).

---

## The one thing you must understand

**WorkBoard's numbers were measured by running the real product.
claude-mem's numbers were NOT produced by running claude-mem — they were
modeled from claude-mem's own published specifications.**

This is an asymmetry, stated up front:

| Side | How its numbers were produced | Status |
|---|---|---|
| **WorkBoard** | Executed the actual product code (`hourly_extractor.py`, `card.py`) against a frozen copy of the real board / real transcripts. | **MEASURED** |
| **claude-mem** | Computed from claude-mem 13.6.1's *own documented* per-layer token costs and per-session compression mechanism. claude-mem was **not installed or run** on the corpus. | **MODELED** |
| **mem0** (where it appears) | Quoted from its published research numbers. | **CITED** |

**Update (2026-06-18):** we *did* then run claude-mem for real, sandboxed — see
`REAL_RUN_FINDINGS.md`. The full stack ran (node22 + Bun + uv + Chroma + worker +
the Stop-hook compression pipeline); it **empirically validated the bootstrap
model's structure** (one compression call per session, reading the full
transcript; no bulk-import mode) and **corrected** a Study C caveat (compression
runs on the main subscription model, not a cheaper tier). The only step that did
not complete was the compression *output* measurement — the sandbox is
intentionally not logged into Claude, so per-observation output token sizes
remain from claude-mem's published spec. So: bootstrap structure = now MEASURED;
recall/observation output sizes = still MODELED.

---

## 1. Why model claude-mem instead of running it?

Three honest reasons — and the honest cost of the choice.

**Reasons it's defensible:**
1. **No "you ran it wrong" rebuttal.** If we installed claude-mem and
   misconfigured it, any unfavorable number is dismissible as our error. Using
   claude-mem's *own claimed* per-layer costs holds it to its own spec.
2. **claude-mem 13.x is heavy to stand up safely** — node ≥20, Bun, uv, Chroma,
   a worker service, and an installer that rewrites `~/.claude/settings.json`
   hooks. A real run must be fully sandboxed to avoid touching the user's live
   Claude Code; that's worth doing deliberately, not casually.
3. **Conservative knobs.** The model's `fragmentation=1.0` gives claude-mem the
   benefit of WorkBoard's consolidation (one observation per card, no
   fragmentation penalty). So the WorkBoard margin is a **floor**, not a peak.

**The cost of the choice (the honest limitation):**
- It is an **estimate of claude-mem, not a measurement.** The model assumes
  claude-mem hits its documented token bands and that retrieval succeeds for
  every query (optimistic for it). Those assumptions are reasonable but
  unverified by us. Treat the head-to-head as "WorkBoard measured vs claude-mem
  per-its-own-spec," and upgrade it with §5 if you need empirical certainty.

---

## 2. Per-number provenance

Every figure in the reports traces to one of these:

| Figure | Source | MEASURED / MODELED / CITED |
|---|---|---|
| WB bootstrap model calls, input tokens (A) | real `hourly_extractor.py` harvest+bucketize in a sandbox `$HOME` | MEASURED |
| claude-mem bootstrap calls = sessions; input = full transcript tokens (A) | claude-mem's SessionEnd-per-session design × corpus transcript tokens | MODELED |
| WB recall index/detail tokens (B) | real `card.py list`/card content vs frozen `board_snapshot.json` | MEASURED |
| claude-mem recall index/detail (B) | claude-mem README: `search` 50-100/result, `get_observations` 500-1000/result | MODELED |
| WB live write = 0 model calls (C) | architectural fact: carding is inline `card.py`, no extra model call | MEASURED/asserted |
| claude-mem live write = 1 call, ~transcript tokens (C) | claude-mem SessionEnd compression design × corpus avg | MODELED |
| WB SessionStart 97, per-turn nudge 306 (C) | real `card.py digest` + the real `hook_user_prompt.sh` template | MEASURED |
| claude-mem SessionStart "~thousands" (C) | docs/TOKEN_BUDGET.md peer table | CITED |
| mem0 6,719 tok/recall (C) | mem0 published benchmark | CITED |

If a number isn't MEASURED, it is labeled in the report and traces to a public
claude-mem/mem0 source — not to our opinion.

---

## 3. Fairness controls (what makes it not cherry-picked)

1. **Same tokenizer, both sides** (`tokencount.py`, `tiktoken cl100k`). The
   single most important control. ~10-15% under Claude's true tokenizer, applied
   identically — so the *ratio* is unaffected.
2. **Same corpus, byte-frozen.** `corpora/<size>/` is fingerprinted by
   `build_fixtures.py`; re-runs use identical input. Excludes the 2026-06-11→15
   inactivity gap so "per day" stays meaningful.
3. **Correctness is real, not a proxy.** A WorkBoard answer counts only if every
   gold fact *literally appears* in a fetched card's content
   (`resolve_answer_cards`, greedy set-cover). 19/20 found; **P06 is a genuine
   board-miss** (a backup-dir name that lives only in a memory file, not on the
   board) — left as an honest miss, and a query claude-mem would likely win.
4. **claude-mem given its best case.** `fragmentation=1.0`; recall assumed to
   succeed every time. The margin is a floor.
5. **Gold answers written before querying** (`queries.json`), to prevent
   judgment drift.

### What the "1383 = 1383" spot-check proves (and doesn't)

It checks that the WorkBoard adapter's reported detail-token count for card #598
matches an independent hand recomputation — i.e. **WorkBoard's measurement
pipeline is faithful.** It says **nothing** about claude-mem (whose numbers are
modeled, §1).

---

## 4. Determinism — anyone gets the same numbers

The harness makes no network calls and no model calls during measurement (recall
reads the frozen snapshot; ingest counts buckets/digests deterministically).
Re-running any driver yields byte-identical results. Verify:

```bash
cd 
python3 run_recall.py | grep reduction      # run twice → identical
python3 run_bootstrap.py | grep medium       # run twice → identical
```

### Safety — the harness never touches the live product

- Recall reads a **frozen copy** `board_snapshot.json`, never the live board.
- Ingest runs in a **throwaway `$HOME`** (symlinked fixture transcripts only).
- Product source is **imported read-only**, never modified (`git status` clean
  for `scripts/`).
- `board_snapshot.json` and `corpora/` are git-ignored (may hold private data);
  only code + `queries.json` + aggregate results ship.

---

## 5. Upgrading the claude-mem side to a REAL run

To replace the modeled claude-mem numbers with measured ones (closing the §1
gap), run claude-mem for real — **fully sandboxed** — per
`run_claude_mem_tiny.md`. In short:

1. Install node ≥20 (standalone, don't touch system node).
2. `export HOME=<throwaway sandbox>` so claude-mem's installer + worker + hook
   rewrites stay inside the sandbox, never the real `~/.claude`.
3. Symlink the **same** `corpora/tiny/transcripts/*.jsonl` into the sandbox.
4. Run claude-mem's bulk compress/ingest; record observations, summaries,
   per-observation token sizes, and SQLite/Chroma bytes into
   `results/raw/claude_mem_tiny_real.json`.
5. Set `claude_mem_adapter.DEFAULTS` from the measured `avg_*` numbers and
   re-render. If they land inside claude-mem's published 50-100 / 500-1000
   bands, the modeled headline stands, now empirically validated. If
   observations fragment (more observations than WorkBoard cards), the WorkBoard
   margin only **widens**.

Until that run is done, every claude-mem figure in this study is **modeled, not
measured** — by design, and disclosed here.

---

## 6. One-command reproduction

```bash
cd 
python3 build_fixtures.py     # freeze corpora from ~/.claude (once)
python3 run_bootstrap.py      # Study A   → results/raw/bootstrap.json
python3 run_recall.py         # Study B   → results/raw/recall.json
python3 replay_session.py     # Study C   → results/raw/live.json
python3 render_report.py      # → REPORT.md
python3 report_bootstrap.py   # → REPORT_BOOTSTRAP.md (detailed A)
python3 report_live.py        # → REPORT_LIVE.md (detailed C)
```
