"""Detailed standalone report for STUDY C — Live carding.

Reads results/raw/live.json (+ bootstrap.json for per-session derivation) and
emits REPORT_LIVE.md with full methodology, the three cost dimensions,
projections, a breakeven analysis, honest caveats and raw appendix.
Run the drivers first (run_bootstrap, run_recall, replay_session), then this.
"""
from __future__ import annotations
import json
from pathlib import Path

BENCH = Path(__file__).resolve().parent
RAW = BENCH / "results" / "raw"
OUT = BENCH / "REPORT_LIVE.md"


def main():
    live = json.loads((RAW / "live.json").read_text())
    boot = json.loads((RAW / "bootstrap.json").read_text())
    med = next((f for f in boot["fixtures"] if f["corpus"] == "medium"),
               boot["fixtures"][0])

    wps = live["memory_write_per_session"]
    proj = live["memory_write_projection"]
    inj = live["context_injection_per_session"]
    pr = live["per_recall"]
    turns = live["scenario"]["turns"]
    cm_write = wps["claude_mem_model_input_tokens"]
    nudge = inj["workboard_per_turn_nudge"]
    nudge_trim = inj["workboard_per_turn_nudge_trimmed"]

    L = []
    w = L.append

    w("# Study C — Live carding: cost to RUN with the memory on")
    w("")
    w(f"> Auto-generated from `results/raw/live.json`. Tokenizer "
      f"`{live['tokenizer']}` (identical for both systems). Card #730. "
      "Part of the WorkBoard vs claude-mem study (`REPORT.md`).")
    w("")
    w("## The question")
    w("")
    w("Bootstrap (Study A) is the one-time build. **Live carding** is the "
      "steady-state cost: while you actually work, what does each system add — in "
      "model calls, model tokens, and injected context — to keep memory current "
      "and answer questions? This is the cost you pay *every session, forever*.")
    w("")
    w("## Three cost dimensions (measured the same way for both)")
    w("")

    # (1) write
    w("### (1) Memory-write — model cost to persist the session's work")
    w("")
    w("| | model calls / session | model input tok / session | "
      f"over {proj['sessions']} sessions |")
    w("|---|--:|--:|--:|")
    w(f"| **WorkBoard** — inline carding | {wps['workboard_model_calls']} | "
      f"{wps['workboard_model_tokens']} | {proj['workboard_write_tokens']:,} |")
    w(f"| **claude-mem** — SessionEnd compress | {wps['claude_mem_model_calls']} | "
      f"{cm_write:,} | {proj['claude_mem_write_tokens']:,} |")
    w("")
    w("**Derivation.** WorkBoard's carding is a deterministic `card.py add/fly` "
      "call; the card's writeup is text the main model already produced during the "
      "turn, so persisting it costs **no extra model call**. claude-mem's SessionEnd "
      "hook runs **one Agent-SDK compression call per session** over the full "
      f"transcript — on the medium corpus that averages **{cm_write:,} input "
      f"tok/session** ({med['cm_ingest_input_tokens']:,} ÷ {med['sessions']} "
      "sessions). Over a project's life this is the bootstrap cost paid again every "
      "session.")
    w("")

    # (2) injection
    w("### (2) Context-injection — interactive tokens added per session")
    w("")
    w(f"| | SessionStart | Per turn | {turns}-turn session | Scales w/ memory size? |")
    w("|---|--:|--:|--:|:--:|")
    w(f"| **WorkBoard** (full nudge) | {inj['workboard_sessionstart']} | {nudge} | "
      f"{inj['workboard_session_inject_full']:,} | **No** (board never auto-loads) |")
    w(f"| **WorkBoard** (trimmed nudge) | {inj['workboard_sessionstart']} | "
      f"{nudge_trim} | {inj['workboard_session_inject_trimmed']:,} | **No** |")
    w(f"| **claude-mem** | {inj['claude_mem_sessionstart']} | — | — | **Yes** |")
    w("")
    w("WorkBoard's injection is a fixed protocol digest + a per-turn nudge; the "
      "130KB+ `board.json` is **never auto-loaded** (CLI-only access), so injection "
      "is **constant in board size**. claude-mem injects a memory block at "
      "SessionStart that grows as stored memory grows (docs/TOKEN_BUDGET.md).")
    w("")

    # (3) recall
    w("### (3) Per-recall — tokens to answer one question (from Study B)")
    w("")
    w("| System | Tokens / recall | vs WorkBoard |")
    w("|---|--:|--:|")
    w(f"| **WorkBoard** (measured) | {pr['workboard_measured']:,} | — |")
    w(f"| claude-mem (spec best-case) | {pr['claude_mem_spec_bestcase']:,} | "
      f"WB {pr['wb_vs_cm_pct']}% lighter |")
    w(f"| mem0 (cited) | {pr['mem0_cited']:,} | WB {pr['wb_vs_mem0_pct']}% lighter |")
    w("")

    # breakeven analysis
    be_full = cm_write / nudge
    be_trim = cm_write / nudge_trim
    w("## The nudge tradeoff — breakeven analysis")
    w("")
    w("WorkBoard's per-turn nudge is its **one heavier surface** — and it's the "
      "lever that makes memory-write free. An honest question: at what session "
      "length does WorkBoard's cumulative nudge overhead equal claude-mem's "
      "single per-session compression call?")
    w("")
    w(f"- Full nudge ({nudge}/turn): breakeven at **~{be_full:.0f} turns** "
      f"({cm_write:,} ÷ {nudge}).")
    w(f"- Trimmed nudge ({nudge_trim}/turn): breakeven at **~{be_trim:.0f} turns** "
      f"({cm_write:,} ÷ {nudge_trim}).")
    w("")
    w("So for sessions shorter than the breakeven, WorkBoard is lighter on this "
      "axis too; for longer sessions its nudge overhead exceeds claude-mem's "
      "compression. **Important caveat:** these token *types differ* — WorkBoard's "
      "nudge is interactive in-context tokens, while claude-mem's compression is a "
      "**separate call**. A real sandboxed run (REAL_RUN_FINDINGS.md) confirmed "
      "that call runs on the **main Claude model via subscription** (the Claude "
      "Agent SDK), *not* a cheaper tier — so it is not discountable. The clean, "
      "rate-independent win is dimension (1): WorkBoard adds **zero model calls** "
      "to persist work; claude-mem adds one full-tier call every session. Trim the "
      "nudge and WorkBoard leads every axis.")
    w("")

    # projection summary
    w("## Putting it together — a 100-session project")
    w("")
    w(f"- **Memory-write model tokens:** WorkBoard **0** vs claude-mem "
      f"**{proj['claude_mem_write_tokens']:,}** ({proj['sessions']} compression "
      "calls).")
    w(f"- **Per recall:** WorkBoard {pr['workboard_measured']:,} vs claude-mem "
      f"{pr['claude_mem_spec_bestcase']:,} ({pr['wb_vs_cm_pct']}% lighter).")
    w("- **SessionStart:** WorkBoard constant ("
      f"{inj['workboard_sessionstart']} tok, board never loaded); claude-mem grows "
      "with memory.")
    w("")

    w("## Honest caveats")
    w("")
    w("- The **per-turn nudge** (306 tok) is real interactive overhead and "
      "WorkBoard's least-flattering surface; it is trimmable to ~40 (TOKEN_BUDGET.md) "
      "but ships at 306 today.")
    w("- claude-mem's per-turn hook injection is **configurable** and not modeled "
      "here (we don't fabricate it); the comparison centers on the per-session "
      "write, which is its documented mechanism.")
    w("- claude-mem's compression buys richer conversational recall; WorkBoard's "
      "free writes capture **work outcomes**, not the whole conversation. Different "
      "value, honestly different cost.")
    w("")

    w("## Reproduce")
    w("")
    w("```bash")
    w("cd ~/Desktop/claude-mem-comparison")
    w("python3 run_bootstrap.py      # provides per-session transcript avg")
    w("python3 run_recall.py         # per-recall numbers")
    w("python3 replay_session.py     # writes results/raw/live.json")
    w("python3 report_live.py        # regenerate this file")
    w("```")
    w("")
    w("## Raw data")
    w("")
    w("```json")
    w(json.dumps(live, indent=2))
    w("```")
    w("")

    OUT.write_text("\n".join(L))
    print(f"wrote {OUT} ({len(L)} lines)")


if __name__ == "__main__":
    main()
