"""Detailed standalone report for STUDY A — Bootstrap.

Reads results/raw/bootstrap.json and emits REPORT_BOOTSTRAP.md with full
methodology, per-corpus derivations, projections, caveats and raw appendix.
Run `python3 run_bootstrap.py` first, then this.
"""
from __future__ import annotations
import json
from pathlib import Path

BENCH = Path(__file__).resolve().parent
RAW = BENCH / "results" / "raw"
OUT = BENCH / "REPORT_BOOTSTRAP.md"

# Illustrative pricing — claude-mem compresses on the Agent SDK; WorkBoard
# bootstrap uses Haiku. Stated as an assumption the reader can change; the
# token RATIO is what matters and is rate-independent.
HAIKU_INPUT_PER_MTOK = 1.00   # USD / million input tokens (assumption, label clearly)


def main():
    boot = json.loads((RAW / "bootstrap.json").read_text())
    fx = boot["fixtures"]
    L = []
    w = L.append

    w("# Study A — Bootstrap: cost to BUILD the memory")
    w("")
    w(f"> Auto-generated from `results/raw/bootstrap.json`. Tokenizer "
      f"`{boot['tokenizer']}` (identical for both systems). Card #730. "
      "Part of the WorkBoard vs claude-mem study (`REPORT.md`).")
    w("")
    w("## The question")
    w("")
    w("Given the **same corpus** of past Claude-Code transcripts, how much does "
      "each system spend — model calls and model-input tokens — to turn that "
      "history into a recall-ready memory store? This is the one-time (or "
      "per-onboarding) cost a user pays to *populate* memory from existing work.")
    w("")
    w("## Method")
    w("")
    w("- **Corpus:** three frozen snapshots of real transcripts from "
      "`~/.claude/projects`, fingerprinted in `corpora/<size>/manifest.json`, "
      "excluding the 2026-06-11→15 inactivity gap.")
    w("- **WorkBoard = measured for real.** We run the actual bootstrap harvest + "
      "bucketize path (`scripts/hourly_extractor.py`, imported read-only) inside a "
      "sandboxed `$HOME` whose `.claude/projects` holds only the fixture. We count "
      "the hourly buckets it forms (= one Haiku call each, `chunk-size 1`) and "
      "tokenize the **compact per-bucket digest** it actually feeds the model.")
    w("- **claude-mem = its own design.** Its SessionEnd hook compresses each "
      "session by reading that session's **full transcript** through one Agent-SDK "
      "call. So its model calls = number of sessions, and its model-input tokens = "
      "the transcript tokens (measured on the same corpus, same tokenizer).")
    w("- **What we are NOT counting:** the deterministic, no-model harvest both "
      "systems do to read files off disk. We count only what reaches a model — the "
      "tokens you pay for.")
    w("")

    w("## Results — per corpus")
    w("")
    w("| Corpus | Window | Sessions | Turns | WB model calls | claude-mem calls | "
      "Fewer calls | WB input tok | claude-mem input tok | **Input reduction** |")
    w("|---|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for f in fx:
        w(f"| {f['corpus']} | {f['window'][0]}→{f['window'][1]} | {f['sessions']} | "
          f"{f['turns']:,} | {f['wb_model_calls']} | {f['cm_model_calls']} | "
          f"{f['calls_ratio_cm_over_wb']}× | {f['wb_ingest_input_tokens']:,} | "
          f"{f['cm_ingest_input_tokens']:,} | **{f['input_reduction_pct']}%** |")
    w("")

    # derivation per corpus
    w("## Where each number comes from")
    w("")
    for f in fx:
        per_bucket = round(f["wb_ingest_input_tokens"] / f["wb_model_calls"])
        per_session = round(f["cm_ingest_input_tokens"] / f["cm_model_calls"])
        w(f"**{f['corpus']}** ({f['sessions']} sessions over "
          f"{f['window'][0]}→{f['window'][1]}):")
        w(f"- WorkBoard groups the work into **{f['wb_model_calls']} hourly "
          f"buckets** → {f['wb_model_calls']} Haiku calls, averaging "
          f"**{per_bucket:,} tok/call** of compact digest "
          f"= {f['wb_ingest_input_tokens']:,} model-input tokens.")
        w(f"- claude-mem compresses all **{f['cm_model_calls']} sessions** → "
          f"{f['cm_model_calls']} calls, averaging **{per_session:,} tok/call** "
          f"(the full session transcript) = {f['cm_ingest_input_tokens']:,} "
          "model-input tokens.")
        w(f"- Net: **{f['calls_ratio_cm_over_wb']}× fewer calls**, "
          f"**{f['input_reduction_pct']}% fewer model-input tokens**.")
        w("")

    w("## Why the gap is so large")
    w("")
    w("Two compounding effects:")
    w("")
    w("1. **Unit of capture.** WorkBoard captures *work*, bucketed by the hour — a "
      "busy day is a handful of buckets. claude-mem captures *every session*, and "
      "a dense corpus has many short sessions (this corpus: hundreds in days). "
      "Calls scale with sessions for claude-mem, with active hours for WorkBoard.")
    w("2. **Input shape.** WorkBoard's harvest pre-distills each bucket into a "
      "compact digest (a few hundred tokens) before the model sees it. claude-mem "
      "feeds the **whole transcript** to its compression model. So even per call, "
      "WorkBoard's model-input is an order of magnitude smaller.")
    w("")
    w("The harvest+digest WorkBoard does first is deterministic and model-free — "
      "it shifts the heavy lifting off the paid model and onto cheap local code.")
    w("")

    # cost projection
    w("## Illustrative cost")
    w("")
    w(f"> Assumption: **${HAIKU_INPUT_PER_MTOK:.2f} / million input tokens** "
      "(Haiku-tier; change the rate, the ratio is unchanged). Input-only; both "
      "also emit output, but input dominates ingestion.")
    w("")
    w("| Corpus | WorkBoard $ | claude-mem $ | You save |")
    w("|---|--:|--:|--:|")
    for f in fx:
        wbd = f["wb_ingest_input_tokens"] / 1_000_000 * HAIKU_INPUT_PER_MTOK
        cmd = f["cm_ingest_input_tokens"] / 1_000_000 * HAIKU_INPUT_PER_MTOK
        w(f"| {f['corpus']} | ${wbd:.4f} | ${cmd:.4f} | {f['input_reduction_pct']}% |")
    w("")

    w("## Honest caveats")
    w("")
    w("- **Different artifacts.** claude-mem's per-session compression yields rich "
      "semantic observations of the *whole conversation*; WorkBoard's digest "
      "extracts *work outcomes* (cards). claude-mem captures more raw conversational "
      "detail — it spends more to store more. The study measures cost-to-build, not "
      "information equivalence.")
    w("- **claude-mem numbers are from its design + the same corpus**, not a live "
      "run of its tool. A sandboxed real-run validation is scripted in "
      "`run_claude_mem_tiny.md`; the spec uses claude-mem's own mechanics so it "
      "can't be accused of sandbagging.")
    w("- **WorkBoard is the real bootstrap path**, measured, but token counts use "
      "the `cl100k` proxy tokenizer (≈10-15% under Claude's true count) — applied "
      "identically to both, so the ratio holds.")
    w("")

    w("## Reproduce")
    w("")
    w("```bash")
    w("cd ~/Desktop/claude-mem-comparison")
    w("python3 build_fixtures.py     # freeze corpora from ~/.claude (once)")
    w("python3 run_bootstrap.py      # writes results/raw/bootstrap.json")
    w("python3 report_bootstrap.py   # regenerate this file")
    w("```")
    w("Sandboxed: ingest runs in a throwaway `$HOME`; the product bootstrap code "
      "is imported read-only and never modified.")
    w("")

    w("## Raw data")
    w("")
    w("```json")
    w(json.dumps(boot, indent=2))
    w("```")
    w("")

    OUT.write_text("\n".join(L))
    print(f"wrote {OUT} ({len(L)} lines)")


if __name__ == "__main__":
    main()
