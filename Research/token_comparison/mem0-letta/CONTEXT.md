# CONTEXT.md — read me first (full context for resuming)

**Purpose of this file:** capture the entire story of this study so a future
session (or a future Claude) has full context **without re-running anything**.
If the session that built this has closed, start here.

---

## 1. What this is (one paragraph)

`WorkBoard/graphify-comparison/` is a standalone, reproducible benchmark that
proves **WorkBoard** (this repo's kanban-of-work memory) is more token-efficient
than the shipping AI-memory systems — **mem0**, **claude-mem**, and **Letta
(MemGPT)** — on the **live memory loop** (the steady-state cost of persisting +
recalling as you work). It exists because those products market big efficiency
numbers ("mem0: 90% fewer tokens", "claude-mem: ~95%") but all of those are
measured **vs a naive full-context baseline, not head-to-head against a peer**.
This study runs the missing head-to-head.

## 2. The headline result (this run)

- **Live loop, 100 sessions × 3 recalls (vs mem0/claude-mem):** WorkBoard
  **33.7% fewer model tokens than mem0**, **52.6% fewer than claude-mem** —
  because mem0/claude-mem spend an extraction/compression LLM call *every session*
  (~5,462 input tok) while WorkBoard's carding is inline/free.
- **Live loop, 100×50×3 (vs Letta, REAL measurement):** WorkBoard **81.0% fewer
  than Letta** (92.2% with a trimmed nudge; 87.1% vs Letta's full in-context) —
  Letta re-sends ~3,444 tok of memory machinery **every turn**.
- **vs full-context (mem0's own baseline):** WorkBoard recall **−90.8%**,
  matching mem0's "90%" on its own terms.
- **HONEST (do NOT over-claim):** WorkBoard does **not** win every single recall.
  mem0's flat ~1,800-tok bundle and Letta's ~1,064-tok archival fetch are *leaner
  per query* than WorkBoard's content-rich cards (2,399). WorkBoard wins the
  **loop** (free writes + 0 memory carried in context), not per-recall. And its
  per-turn nudge (306 tok) must be trimmed (→40) to win all-in on long sessions.

## 3. Card lineage (board #s)

- **#730** — original WorkBoard vs **claude-mem** study (in `docs/study_2026_06/`).
- **#734** — **mem0** extension (built the standalone `~/Desktop/graphify-comparison/`).
- **#735** — **Letta** extension (4th peer, REAL measurement).
- **#738** — **THIS task:** transferred the whole folder into
  `WorkBoard/graphify-comparison/`, made `REPORT_DETAILED.md`, wrote this doc.

Full detail also in memory: `project-workboard-vs-clauedmem-study`.

## 4. Step-by-step — what was actually done

**Session A (#730):** built the harness in `docs/study_2026_06/` — shared
tokenizer (`tokencount.py`, tiktoken cl100k), frozen fingerprinted corpora
(`build_fixtures.py` from `~/.claude`), 20 gold queries (`queries.json`, answers
pre-written), real WorkBoard recall via `card.py` against a frozen
`board_snapshot.json`, claude-mem modeled from its README. Result: bootstrap
~99% fewer tokens, recall ~26% fewer (33% lifecycle).

**Session B (#734):** user asked to beat **mem0** and to **NOT touch the product**
→ copied the harness OUT to `~/Desktop/graphify-comparison/`, repointed it at
read-only copies (`lib/card_ro.py`, `lib/product_scripts_ro/`), added
`peers/mem0_adapter.py` (modeled from arXiv:2504.19413 + mem0.ai/research-3:
flat ~1,800 tok/recall, 1 ADD extraction call/session), wrote `run_live.py`
(PRIMARY), made recall/bootstrap/report 3-way, added `lib/safety.py` guard.

**Session C (#735):** added **Letta** as a 4th peer, measured REAL from Letta
0.16.8's shipped artifacts (system prompt `memgpt_v2_chat`, tool JSON schemas,
`Memory.compile()`): 3,444 tok/turn in-context. Files: `peers/letta_adapter.py`,
`letta_incontext_real.py`, `letta_real_run.py` (live Docker+Ollama corroboration),
`run_live_letta.py`, Study 1b in the report. Also a `calibration.json` bonus axis
vs **graphify** (graphifyy 0.8.40, a code knowledge-graph).

**Session D (#738) — THIS one:**
1. `rsync`'d the whole folder `~/Desktop/graphify-comparison/` →
   `WorkBoard/graphify-comparison/`, **excluding** `.letta-venv/` (891MB, regen),
   `__pycache__/`, `peers/_wb_ingest_home/`.
2. Rewrote `lib/safety.py`: the folder is now INSIDE the repo by design, so the
   guard changed from "must be outside product" to "writes confined to this
   subfolder; never the live `board/board.json` or product source elsewhere."
3. Updated `.gitignore` to also ignore `.letta-venv/` + `.letta-state/`.
4. Verified the full pipeline reproduces from the new path + is deterministic
   (byte-identical re-render); live `board.json` untouched by the study.
5. Built `render_report_detailed.py` → `REPORT_DETAILED.md` (14 sections, all
   peers, full tables, sources, limitations) — derived from JSON, deterministic.
6. Wrote this `CONTEXT.md`.

## 5. File map

| File | What it is |
|---|---|
| `REPORT.md` | short auto-generated report (the deliverable) |
| `REPORT_DETAILED.md` | exhaustive auto-generated report (all peers, sources, limits) |
| `CONTEXT.md` | **this file** — full story for resuming |
| `README.md` | harness usage + fairness rules |
| `tokencount.py` | single shared tokenizer (fairness control) |
| `build_fixtures.py` | freezes `~/.claude` transcripts → `corpora/` (read-only on source) |
| `corpus_stats.py` | sessions/turns/transcript-tokens per corpus |
| `queries.json` | 20 gold queries + pre-written answers |
| `peers/workboard_adapter.py` | REAL WorkBoard ingest + recall + correctness |
| `peers/mem0_adapter.py` | mem0 modeled from its published numbers |
| `peers/claude_mem_adapter.py` | claude-mem modeled from its README |
| `peers/letta_adapter.py` | Letta modeled (structural) |
| `letta_incontext_real.py` | REAL Letta per-turn measurement from shipped artifacts (needs `.letta-venv`) |
| `letta_real_run.py` | optional LIVE Letta server corroboration (Docker+Ollama) |
| `run_recall.py` | Study 2 → `results/raw/recall.json` |
| `run_live.py` | Study 1 (mem0/claude-mem) → `results/raw/live.json` |
| `run_live_letta.py` | Study 1b (Letta) → `results/raw/live_letta.json` |
| `run_bootstrap.py` | Study 3 → `results/raw/bootstrap.json` |
| `render_report.py` | regenerates `REPORT.md` from raw JSON |
| `render_report_detailed.py` | regenerates `REPORT_DETAILED.md` from raw JSON |
| `lib/safety.py` | non-invasiveness guard + snapshot fingerprint |
| `lib/card_ro.py` | read-only copy of product `card.py` |
| `lib/product_scripts_ro/` | read-only copy of product `scripts/` (ingest path) |
| `results/raw/*.json` | every computed number (git-ignored, present locally) |
| `board_snapshot.json` | frozen board copy (git-ignored — may hold secrets) |
| `corpora/` | frozen transcript fixtures (git-ignored — heavy/private) |

## 6. How to RE-RETRIEVE without re-running (the cheap path)

The computed numbers already live in `results/raw/*.json` and both reports are
written. To just **re-read the results**: open `REPORT_DETAILED.md`. To
**re-render** the reports from existing JSON (no model calls, instant):
```bash
cd WorkBoard/graphify-comparison
python3 render_report.py && python3 render_report_detailed.py
```

To **fully re-derive** (needs `~/.claude` for corpora):
```bash
python3 build_fixtures.py && python3 run_recall.py && python3 run_live.py \
  && python3 run_bootstrap.py && python3 render_report.py && python3 render_report_detailed.py
```

To re-derive **Letta** (Study 1b) — the venv was NOT transferred (891MB):
```bash
python3.11 -m venv .letta-venv && .letta-venv/bin/pip install letta
.letta-venv/bin/python letta_incontext_real.py && python3 run_live_letta.py
```

## 7. Gotchas / threats (so future-you doesn't trip)

- **`.letta-venv/` is git-ignored and NOT in the transfer.** Recreate it (above)
  to re-run Letta's real measurement. The Letta *results* JSON ARE present, so the
  report renders without it.
- **`board_snapshot.json` + `corpora/` are git-ignored** (private). A fresh clone
  re-derives them from `board/board.json` + `~/.claude`. Locally they're present.
- **mem0/claude-mem are modeled** from their own published numbers (best-case for
  them) — NOT run. Letta is the one measured for real.
- **Do not over-claim:** the win is the *loop*, not per-recall (mem0 & Letta are
  leaner per query). The nudge must be trimmed for long sessions. See
  `REPORT_DETAILED.md` §11 (limitations).
- **tiktoken ≈ 10–15% under Claude's real tokenizer**, applied to all systems →
  ratios unaffected.
- **Non-invasive:** the study writes only under this subfolder; `lib/safety.py`
  refuses to write the live board. The live `board.json` is untouched by runs.

## 8. If a user asks "is WorkBoard better than <peer>?"

Answer with the loop number + the honest caveat. Template:
> On real history, head-to-head, WorkBoard runs the live memory loop with **34%
> fewer tokens than mem0 / 53% than claude-mem / 81% than Letta**, because its
> writes are free and it carries no memory in context. Per *single* recall, mem0
> and Letta are actually leaner — WorkBoard wins the loop, not the lookup. And its
> "−90.8% vs full-context" matches mem0's own "90%" headline on mem0's own baseline.
