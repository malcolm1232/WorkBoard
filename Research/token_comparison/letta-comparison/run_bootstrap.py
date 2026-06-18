"""STUDY A driver — bootstrap ingestion cost, WorkBoard vs claude-mem.

For each fixture: measure what each system spends to turn the SAME corpus into a
recall-ready memory artifact.

  WorkBoard  : real harvest+bucketize (deterministic, no model) → counts hourly
               buckets/chunks = model calls, and the compact per-bucket digest
               tokens it feeds Haiku.
  claude-mem : modeled from its design — one compression Agent-SDK call per
               session, reading the full transcript tokens.

The asymmetry IS the finding: WorkBoard does cheap deterministic filtering
(harvest+digest) before spending model tokens; claude-mem feeds whole
transcripts to a model per session.

Writes results/raw/bootstrap.json. Reads only frozen fixtures + sandbox HOME.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(BENCH_DIR / "peers"))
import corpus_stats as cs            # noqa: E402
import workboard_adapter as wb       # noqa: E402
import claude_mem_adapter as cm      # noqa: E402
import mem0_adapter as m0            # noqa: E402
import tokencount                    # noqa: E402

RESULTS = BENCH_DIR / "results" / "raw"
CORPORA = BENCH_DIR / "corpora"


def run(fixtures=("tiny", "medium", "large")) -> dict:
    out = {"tokenizer": tokencount.backend_name(), "fixtures": []}
    for fx in fixtures:
        cdir = CORPORA / fx
        if not cdir.exists():
            continue
        stats = cs.corpus_stats(cdir)
        wbi = wb.ingest_estimate(cdir)
        cmi = cm.ingest_spec(stats)
        m0i = m0.ingest_spec(stats)

        wb_calls = wbi["model_calls"]
        cm_calls = cmi["model_calls"]
        m0_calls = m0i["model_calls"]
        wb_in = wbi["ingest_input_tokens"]
        cm_in = cmi["ingest_input_tokens"]
        m0_in = m0i["ingest_input_tokens"]
        out["fixtures"].append({
            "corpus": fx,
            "window": stats["window"],
            "sessions": stats["sessions"],
            "turns": stats["turns"],
            "transcript_tokens": stats["transcript_tokens"],
            "wb_model_calls": wb_calls,
            "cm_model_calls": cm_calls,
            "m0_model_calls": m0_calls,
            "wb_ingest_input_tokens": wb_in,
            "cm_ingest_input_tokens": cm_in,
            "m0_ingest_input_tokens": m0_in,
            "calls_ratio_cm_over_wb": round(cm_calls / wb_calls, 1) if wb_calls else None,
            "calls_ratio_mem0_over_wb": round(m0_calls / wb_calls, 1) if wb_calls else None,
            "input_reduction_pct": round((1 - wb_in / cm_in) * 100, 1) if cm_in else None,
            "input_reduction_vs_mem0_pct": round((1 - wb_in / m0_in) * 100, 1) if m0_in else None,
            "wb_buckets": wbi["buckets"],
        })
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "bootstrap.json").write_text(json.dumps(out, indent=2))
    return out


def _print(out):
    print(f"tokenizer: {out['tokenizer']}\n")
    for f in out["fixtures"]:
        print(f"=== {f['corpus']}  ({f['window'][0]} → {f['window'][1]}, "
              f"{f['sessions']} sessions, {f['turns']} turns) ===")
        print(f"  model calls : WorkBoard {f['wb_model_calls']:>5}  vs  "
              f"mem0 {f['m0_model_calls']:>5} ({f['calls_ratio_mem0_over_wb']}× fewer)  vs  "
              f"claude-mem {f['cm_model_calls']:>5} ({f['calls_ratio_cm_over_wb']}× fewer)")
        print(f"  model input : WorkBoard {f['wb_ingest_input_tokens']:>9,}  vs  "
              f"mem0 {f['m0_ingest_input_tokens']:>11,} ({f['input_reduction_vs_mem0_pct']}% fewer)  vs  "
              f"claude-mem {f['cm_ingest_input_tokens']:>11,} ({f['input_reduction_pct']}% fewer)")
        print()


if __name__ == "__main__":
    _print(run())
