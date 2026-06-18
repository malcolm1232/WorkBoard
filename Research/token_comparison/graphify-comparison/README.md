# graphify-comparison — WorkBoard vs graphify (live operating cost)

Standalone, reproducible benchmark comparing **WorkBoard** against **graphify**
(`github.com/safishamsi/graphify`, PyPI `graphifyy`). 3rd peer in
`Research/token_comparison/`. Self-contained: everything needed to regenerate the
reports lives in this one folder.

> **New reader / resuming?** Read **`CONTEXT.md`** first — it has the full story
> and every number, so you don't need to re-run anything.

## Which document do I read? (structure)

The docs go from shortest/plainest to most detailed — pick by what you need:

| File | What it is | Read it when… |
|---|---|---|
| **`README.md`** | this index — headline + file map + how to reproduce | you're orienting / first landing here |
| **`OVERVIEW.md`** | plain-language explainer: how Graphify works, how WorkBoard works, the verdict, the method, strengths/weaknesses (no setup needed) | you want to *understand* the comparison without numbers-diving |
| **`REPORT.md`** | the concise honest report — measured table + conclusion (the shareable deliverable) | you want the result + the key numbers |
| **`REPORT_DETAILED.md`** | full report — every measured constant, component-by-component verdicts, fairness controls, "why the original premise was wrong" | you're auditing or defending the numbers |
| **`CONTEXT.md`** | resume-without-rerunning doc — full build story, step-by-step, every number inlined, caveats, card lineage | you're a future session picking this up cold |
| **`REPRODUCIBILITY.md`** | determinism, what's git-ignored, version pin, non-invasiveness | you want to re-run or trust the repro |
| **`measure_graphify_real.md`** | the sandboxed real-run recipe (venv → graph → calibration) | you want to re-measure Graphify from scratch |

Code & data: `run_live_graphify.py` + `render_report.py` regenerate the reports
from `results/raw/{calibration,live}.json`; `peers/` holds the two cost surfaces;
`fixtures/` holds the frozen inputs (corpus, graph, WorkBoard SKILL.md).

## Headline (honest)

**graphify is a lightweight peer, not a heavyweight — there is no 95%-style win.**
claude-mem/mem0/Letta lose the live loop because they spend LLM tokens to *write*
memory; graphify does not (local tree-sitter AST, 0 API tokens — like WorkBoard's
`card.py`). The real difference is **shape**: WorkBoard remembers *work outcomes*,
graphify remembers *code structure*. Complements, not competitors.

| Axis (tiktoken cl100k) | WorkBoard | graphify | Lighter |
|---|--:|--:|---|
| Always-on / prompt | 306 nudge | 61 cached | **graphify** |
| SKILL.md on engage | **5,898** | 8,245 (+9,704 refs) | **WorkBoard (28.5%)** |
| Per recall | 2,399 (work Qs) | 1,374 (code Qs) | different questions |
| Write / keep current | 0 | 0 | tie |
| Big-artifact autoload | never | never | tie |

## Files

```
graphify-comparison/
├── README.md                 (this index + document-structure guide)
├── OVERVIEW.md               plain-language explainer (no setup needed)
├── CONTEXT.md                full story + every number (read first)
├── REPORT.md                 concise honest report (deliverable)
├── REPORT_DETAILED.md        full report (all constants, verdicts, fairness)
├── REPRODUCIBILITY.md        how to reproduce + what's git-ignored
├── measure_graphify_real.md  sandboxed real-run recipe
├── run_live_graphify.py      composes calibration -> results/raw/live.json
├── render_report.py          live.json + calibration -> REPORT*.md
├── tokencount.py             shared tokenizer (tiktoken cl100k = fairness)
├── peers/
│   ├── graphify_live.py      graphify cost surface (measured)
│   └── workboard_live.py     WorkBoard cost surface (measured)
├── lib/safety.py             write-confinement guard (in-repo variant)
├── fixtures/
│   ├── code_corpus/          frozen WorkBoard/scripts/*.py (graphify's input)
│   │   └── graphify-out/graph.json   the real graph (710 nodes / 1396 edges)
│   └── workboard_SKILL.md    frozen copy of WorkBoard/SKILL.md
├── results/raw/              calibration.json + live.json   [git-ignored]
├── board_snapshot.json       frozen product board            [git-ignored]
└── sandbox/                  throwaway venv + HOME           [git-ignored]
```

## Reproduce

```bash
# offline (seconds) — from cached calibration:
python3 run_live_graphify.py && python3 render_report.py
# from scratch (re-measure graphify): see measure_graphify_real.md
```

Card **#733**. Nothing here touches the live product — graphify runs under a
sandbox `$HOME`; the harness only reads frozen copies and writes inside this
folder (`lib/safety.py` enforces it).
