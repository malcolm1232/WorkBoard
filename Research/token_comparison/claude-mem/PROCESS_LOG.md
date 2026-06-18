# PROCESS_LOG — what was done, step by step

Purpose: a future session (or person) can read this and have **full context
without rerunning anything**. It records every step, why, the obstacles, the
decisions, and what each step yielded. If a session closes and context is lost,
start here.

Origin: user asked "claude-mem made a 95% efficiency claim — can we make a
similar quantified claim and prove ours is more efficient?" Then: keep it
isolated from the product, make it reproducible, run claude-mem for real, and
finally consolidate everything into this one folder.

Cards: #730 (study), #733 (graphify deferred), #736 (reproducibility doc),
#737 (real run), #739 (this consolidation).

---

## 0. Mental model

Three questions, three studies:
- **A — Bootstrap:** cost to *build* memory from past transcripts.
- **B — Recall:** tokens to *answer* a question from memory (the headline).
- **C — Live carding:** steady-state cost to *persist* work as you go.

Comparison target: **claude-mem** (named by the user; made a "~95%" claim).
mem0 is cited for context. graphify deferred to a later session (#733).

---

## 1. Built the harness (in-product first, later moved here)

- `tokencount.py` — ONE shared tokenizer (`tiktoken cl100k`) used for every
  count on both sides. This is the single most important fairness control.
  (First attempt was named `tokenize.py` — shadowed Python's stdlib; renamed.)
- `build_fixtures.py` — froze three corpora from `~/.claude/projects/-Users-malco/*.jsonl`
  into `corpora/{tiny,medium,large}/`, each fingerprinted in `manifest.json`.
  Windows **exclude the 2026-06-11→15 inactivity gap** (user was away; idle days
  would distort "per day"). tiny=336 files/Jun16-17, medium=933/May28-Jun10,
  large=984/May17-Jun10.
- `queries.json` — 20 recall questions (7 pinpoint / 7 thematic / 6 lifecycle)
  with **gold answers written before any querying**. Gold facts are hard anchors
  (card #s, commit shas, versions) verified to be card-resident.

## 2. WorkBoard adapter — MEASURED for real

`peers/workboard_adapter.py`:
- **Recall:** real two-layer retrieval against a frozen `board_snapshot.json`
  (never the live board). Index = grep of `card.py list`; detail = a **compact
  card payload** (title/origin/writeup/links/subtasks — NOT the full history
  metadata, which a recall never needs). Correctness is REAL: a query counts
  only if every gold fact literally appears in a fetched card
  (`resolve_answer_cards`, greedy set-cover). Result: **19/20 found**; P06 is a
  genuine board-miss (a backup-dir name that lives only in a memory file).
- **Ingest:** runs the real bootstrap harvest+bucketize (`hourly_extractor.py`)
  in a throwaway `$HOME` holding only the fixture, counting hourly buckets
  (= Haiku calls) and tokenizing the compact per-bucket digests. No model calls.
- Fix along the way: `_bucketize` returns `(keys, dict, chunks)`, not tuples;
  and a `#N` gold ref must resolve to that exact card, not any card mentioning
  the digits (P05 bug — fixed).

## 3. claude-mem adapter — MODELED from its own published numbers

`peers/claude_mem_adapter.py`:
- Uses claude-mem 13.6.1's README economics: `search` ~50-100 tok/result,
  `get_observations` ~500-1,000 tok/result, one compression call per session
  over the full transcript.
- `fragmentation=1.0` (gives claude-mem WorkBoard's consolidation benefit) →
  the WorkBoard margin is a conservative floor.
- Why model, not run: holds claude-mem to its OWN numbers (no "you ran it wrong"
  rebuttal); claude-mem 13.x is heavy + its installer rewrites `~/.claude` hooks.

## 4. Drivers + results

- `run_bootstrap.py` → `results/raw/bootstrap.json` (Study A)
- `run_recall.py` → `results/raw/recall.json` (Study B, headline)
- `replay_session.py` → `results/raw/live.json` (Study C)
- `corpus_stats.py` — sessions/turns/transcript-tokens per corpus (shared input).

**Findings:**
- A: tiny 99.2% / medium 98.7% / large 98.6% fewer model-input tokens;
  5.1–15.3× fewer model calls.
- B: WorkBoard 2,399 vs claude-mem 3,237 tok mean = **25.9% fewer**, wins 16/19
  (pinpoint 14.6%, thematic 26.2%, lifecycle 32.6%).
- C: 0 vs 5,462 model tokens/session to persist; 0 vs 546,200 over 100 sessions.
  Nudge breakeven ~18 turns (full) / ~137 (trimmed).

## 5. Reports

Mechanically generated from the JSON (never hand-typed numbers):
- `render_report.py` → `REPORT.md` (combined)
- `report_bootstrap.py` → `REPORT_BOOTSTRAP.md`
- `report_live.py` → `REPORT_LIVE.md`
- `report_full.py` → `REPORT_FULL.md` (everything, most detailed)

## 6. Reproducibility rules (#736)

`REPRODUCIBILITY.md` — leads with the honest disclosure: **WorkBoard measured,
claude-mem modeled from its docs**. Per-number provenance table tags every
figure MEASURED / MODELED / CITED. Clarifies the "1383 = 1383" spot-check only
validates WorkBoard's pipeline.

## 7. Real claude-mem run (#737) — sandboxed, on node 22

To remove the "modeled not measured" asymmetry:
- Downloaded standalone **node 22** to `/tmp` (system node 18 untouched).
- `claude-mem install` into throwaway `$HOME=/tmp/cm-real-home` → auto-installed
  **Bun 1.3.14 + uv 0.11.21**, started the worker (:37701). Real `~/.claude`
  never touched (verified).
- Symlinked the **same frozen tiny transcripts**; triggered the real compression
  path (Stop hook → `worker-service.cjs summarize` → Claude Agent SDK).

**Wall:** the SDK returned `Not logged in · Please run /login` — the sandbox is
deliberately not logged into Claude, and extracting host credentials is unsafe
(and was blocked). So the compression *model call* couldn't complete.

**But it validated the STRUCTURE (now measured, not modeled):**
1. One compression call per session, reading the full transcript → confirms
   Study A's `calls = sessions`, `input = full transcript` (basis of ~99%).
2. claude-mem has **no bulk/bootstrap mode** — per-session live hooks only.
3. Compression runs on the **main Claude model via subscription**, NOT a cheap
   tier → corrected Study C; strengthens WorkBoard's 0-call advantage.

Still modeled (auth-blocked): per-observation OUTPUT token sizes — kept from
claude-mem's published spec. To finish: `/login` inside the sandbox, re-trigger
summarize on the 14 substantial sessions, read `~/.claude-mem/claude-mem.db`.
See `REAL_RUN_FINDINGS.md` + `run_claude_mem_tiny.md`.

Cleanup: worker stopped, `/tmp/cm-real-home` removed, isolation verified.

## 8. Consolidation into this folder (#739)

- Copied the whole harness out of the product into `~/Desktop/claude-mem-comparison/`.
- **Vendored** `WorkBoard/scripts/*.py` (37 files) read-only under
  `lib/product_scripts_ro/` so the folder runs WITHOUT the product; rewired
  `workboard_adapter.py` to use it.
- Copied frozen `board_snapshot.json` + `corpora/` (492 MB, local) + cited
  `reference/TOKEN_BUDGET.md` + `reference/COMPARISON.md`.
- Re-ran every driver from here → **identical numbers** (deterministic).
- Removed the in-product copy `docs/study_2026_06/` (cleanup) and the `/tmp`
  sandboxes.

---

## End product — what this folder yields

Running the 7 commands in `README.md` reproduces, with no product and no network:
- `results/raw/{bootstrap,recall,live}.json` — the raw numbers.
- `REPORT_FULL.md` / `REPORT.md` / `REPORT_BOOTSTRAP.md` / `REPORT_LIVE.md` —
  the written reports, numbers mechanically derived from the JSON.

Bottom line the study supports:
- **Bootstrap:** WorkBoard ~99% fewer ingest tokens than claude-mem (head-to-head,
  structure empirically validated) — meets/exceeds claude-mem's own "95%".
- **Recall:** ~26% fewer tokens to answer (33% lifecycle), conservatively.
- **Live:** 0 vs hundreds of thousands of model tokens to keep memory current.
- **Honest:** claude-mem wins vague/off-board recall + tight single-fact
  pinpoints; per-observation output sizes remain its published spec until a
  `/login`-authorized real run measures them.
