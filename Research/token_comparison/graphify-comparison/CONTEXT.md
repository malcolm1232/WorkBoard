# CONTEXT.md — read me first (full context for resuming)

**Purpose of this file:** capture the entire story of this study so a future
session (or a future Claude) has full context **without re-running anything**. If
the session that built this has closed, start here. Every key number is inlined
below so it survives even if the git-ignored raw data (`results/raw/`,
`board_snapshot.json`, `sandbox/`) is absent on a fresh clone.

---

## 1. What this is (one paragraph)

`WorkBoard/Research/token_comparison/graphify-comparison/` is a standalone,
reproducible benchmark comparing **WorkBoard** (this repo's kanban-of-work memory)
against **graphify** (`github.com/safishamsi/graphify`, PyPI `graphifyy`) on the
**live operating cost** of having the system on as you work. It is the 3rd peer in
the suite (after claude-mem and the mem0/Letta study). Unlike those, the result is
**not** a big WorkBoard efficiency win — and that honest finding is the point.

## 2. The headline result (this run)

- **graphify is a LIGHTWEIGHT peer, not a heavyweight.** There is **no
  95%-style efficiency win vs graphify.** claude-mem/mem0/Letta lose the live loop
  because they spend LLM tokens to *write* memory (per session, or per turn for
  Letta). graphify does **not**: its graph is built by local tree-sitter AST at
  **0 API tokens** — the same order as WorkBoard's deterministic `card.py` write.
- **Per-prompt always-on:** graphify **0** (it injects nothing) vs WorkBoard
  **306**/prompt (the nudge). On this axis graphify is *lighter*.
- **SKILL.md on engagement:** WorkBoard **5,898** vs graphify **8,245**
  (+9,704 references on demand) → WorkBoard **28.5% lighter**.
- **Per recall:** WorkBoard 2,399 (work-outcome Qs) vs graphify 1,374 (code-graph
  Qs) — **different questions, not a head-to-head.**
- **Write / big-artifact autoload:** both **0 / 0** → ties.
- **The real difference is SHAPE:** WorkBoard records *work outcomes* (what
  shipped / why / links / lifecycle); graphify records *code structure* (what
  calls what). Complements, not competitors.

## 3. Why the original premise was wrong (important)

The plan started from the rendered GitHub page's claim that graphify uses a
**PreToolUse hook that fires before every file read** — which would have made its
live tax scale with tool-calls/prompt (a big WorkBoard win). **The real install
disproves this.** `graphify install --platform claude` (run sandboxed) writes only
a SKILL + one CLAUDE.md line + on-demand references; **no `settings.json`, no hook
entry.** Its optional `graphify hook install` is a *git post-commit* rebuild hook
(local AST), not a context-injection hook. We measured rather than assumed — and
the measurement killed the headline. This is why the report lands as a trade-off.

## 4. Card lineage (board #s)

- **#730** — WorkBoard vs **claude-mem** study (`docs/study_2026_06/`).
- **#734 / #735** — **mem0 + Letta** live-loop extension (`letta-comparison/`).
- **#733** — **THIS task:** add **graphify** as a peer; built this folder,
  real sandboxed measurement, honest trade-off report + this doc.

## 5. Step-by-step — what was actually done

1. **Researched graphify** (web + the actual PyPI package). Found: code-structure
   knowledge graph (tree-sitter AST → `graph.json`), queried by loading a
   matching BFS subgraph. PyPI name is `graphifyy`; needs Python 3.10+.
2. **Froze inputs into this folder:** `fixtures/code_corpus/` = a copy of
   `WorkBoard/scripts/*.py` (37 files) as the code graphify ingests;
   `fixtures/workboard_SKILL.md` = frozen copy of `WorkBoard/SKILL.md`;
   `board_snapshot.json` = frozen product board (same as sibling studies);
   `tokencount.py` + `lib/safety.py` copied from the sibling study.
3. **Real sandboxed graphify run** (`measure_graphify_real.md`): throwaway
   `python3.13` venv, `pip install graphifyy` (0.8.41), `export HOME=sandbox/home`,
   `graphify install --platform claude` → inspected the footprint (no hook!),
   `graphify .` → built graph (710 nodes / 1396 edges), ran a 5-query set →
   captured BFS subgraph payloads.
4. **Tokenized everything** with the shared cl100k tokenizer → wrote
   `results/raw/calibration.json` (the single source of measured truth).
5. **Modeled the live session** (`run_live_graphify.py` → `results/raw/live.json`):
   per-session components + a per-prompt sweep (T=1/10/50/100).
6. **Rendered reports** (`render_report.py` → `REPORT.md` + `REPORT_DETAILED.md`).
7. **Honest framing decided with the user:** headline = live injection tax, no
   forced single %, report both nudge values (306 + trimmed 40) and recommend the
   trim. When the data refused a WorkBoard win, we published the trade-off instead
   of manufacturing a number.

## 6. The end product — what this folder yields

- **`REPORT.md`** — concise honest report (the shareable deliverable).
- **`REPORT_DETAILED.md`** — full report: every measured constant, component
  verdicts, fairness controls, the "why the premise was wrong" section.
- **`results/raw/calibration.json`** — measured constants (graphify + WorkBoard).
- **`results/raw/live.json`** — composed session model + sweep.
- **`fixtures/code_corpus/graphify-out/graph.json`** — the real graph (evidence).
- **`measure_graphify_real.md`** — re-measure recipe.
- **`CONTEXT.md`** (this) + **`README.md`** + **`REPRODUCIBILITY.md`**.

## 7. How to reproduce (two levels)

- **Offline (seconds), no graphify needed** — regenerate the model + reports from
  the committed/cached calibration:
  ```bash
  python3 run_live_graphify.py && python3 render_report.py
  ```
- **From scratch (re-measure graphify)** — rebuild the venv + graph + calibration:
  follow `measure_graphify_real.md`, then run the two commands above.

## 8. Key caveats / do-NOT-overclaim

- Do **not** claim "WorkBoard is X% more efficient than graphify." The data does
  not support it; graphify shares WorkBoard's 0-token-write architecture.
- graphify's per-recall (1,374) is *leaner* than WorkBoard's (2,399), but they
  answer different questions — never present that as a WorkBoard loss either.
- Pin: numbers are **graphifyy 0.8.41**. If a future version adds a real
  PreToolUse hook, the per-prompt row changes — re-measure.
- Provenance of WorkBoard's `recall_mean_tok` (2,399): reused from the claude-mem
  study on the same frozen board; it is the only constant not re-derived here.
