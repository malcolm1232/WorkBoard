"""PRIMARY (Letta track) — WorkBoard vs Letta, live memory loop.

Letta's cost shape is structurally different from mem0/claude-mem, so it gets its
own driver instead of being forced into the per-session extraction table.

  mem0 / claude-mem : pay ONCE per session (an extraction/compression LLM call).
  Letta             : pays EVERY TURN — its core memory blocks + memory-tool JSON
                      schemas + MemGPT system prompt are re-sent to the model on
                      every interaction (docs.letta.com memory-blocks: blocks are
                      "always visible … prepended to the prompt every interaction").
                      Plus each memory write is an LLM tool call, plus a Haiku
                      compaction call when context fills.
  WorkBoard         : carries NO memory in context (board never auto-loaded); its
                      only per-turn surface is the protocol nudge (306 tok,
                      trimmable to 40). Writes are a deterministic `card.py` call —
                      0 model tokens.

So the fair head-to-head is the PER-TURN memory overhead. Letta numbers come from
peers/letta_adapter.py, which reads the REAL local run (letta_real.json) when
present, else a documented spec. We report two Letta figures, honestly:
  - memory-attributable  = core blocks + memory-tool schemas (the apples-to-apples
                           surface vs WorkBoard's nudge)
  - full in-context      = + MemGPT system prompt (the whole carried payload)

Writes results/raw/live_letta.json. Standalone & non-invasive (safety guard).
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH / "peers"))
sys.path.insert(0, str(BENCH / "lib"))
import tokencount            # noqa: E402
import run_recall           # noqa: E402
import letta_adapter as lt  # noqa: E402
import safety               # noqa: E402

RESULTS = BENCH / "results" / "raw"

# MEASURED WorkBoard live surfaces (same constants as run_live.py)
WB_SESSIONSTART = 97
WB_PER_TURN_NUDGE = 306
WB_NUDGE_TRIMMED = 40
WB_WRITE_MODEL_TOKENS = 0          # inline deterministic carding


def run(turns: int = 50, sessions: int = 100, recalls_per_session: int = 3) -> dict:
    safety.assert_outside_product()

    live = lt.live({})
    bd = live.get("per_turn_incontext_breakdown") or {}
    # memory-attributable per-turn overhead = blocks + memory-tool schemas
    mem_blocks = bd.get("blocks", 0) or 0
    mem_tool_schemas = bd.get("memory_tool_schemas", bd.get("tool_schemas", 0)) or 0
    letta_mem_per_turn = mem_blocks + mem_tool_schemas
    letta_full_per_turn = live.get("per_turn_incontext_tokens", 0) or 0
    letta_blocks_full = bd.get("blocks_full_capacity")  # worst-case 5000-char fill

    # recall (real if measured, else spec)
    rec = run_recall.run()
    found = [r for r in rec["rows"] if r["wb_found"]]
    wb_recall = round(sum(r["wb_total"] for r in found) / len(found))
    lt_recall = lt.recall({"id": "agg", "shape": "thematic"})["total_tokens"]

    N, T, K = sessions, turns, recalls_per_session

    # (1) PER-TURN memory overhead — the head-to-head -------------------------
    per_turn = {
        "letta_memory_attributable": letta_mem_per_turn,
        "letta_full_incontext": letta_full_per_turn,
        "letta_blocks_full_capacity_per_turn": letta_blocks_full,
        "letta_breakdown": bd,
        "workboard_nudge_full": WB_PER_TURN_NUDGE,
        "workboard_nudge_trimmed": WB_NUDGE_TRIMMED,
        "workboard_in_context_memory": 0,
        "model": live.get("model"),
        "backend": live.get("backend"),
        "note": "Letta re-sends memory blocks + memory-tool schemas every turn; "
                "WorkBoard carries no memory in context, only the protocol nudge.",
    }

    # (2) LIVE LOOP over a project lifetime ----------------------------------
    # memory-attributable in-context tax over N sessions × T turns
    letta_mem_io = N * (letta_mem_per_turn * T + lt_recall * K)
    letta_full_io = N * (letta_full_per_turn * T + lt_recall * K)
    wb_io_full = N * (WB_SESSIONSTART + WB_PER_TURN_NUDGE * T + WB_WRITE_MODEL_TOKENS + wb_recall * K)
    wb_io_trim = N * (WB_SESSIONSTART + WB_NUDGE_TRIMMED * T + WB_WRITE_MODEL_TOKENS + wb_recall * K)
    loop = {
        "sessions": N, "turns": T, "recalls_per_session": K,
        "letta_memory_attributable_tokens": letta_mem_io,
        "letta_full_incontext_tokens": letta_full_io,
        "workboard_tokens_full_nudge": wb_io_full,
        "workboard_tokens_trimmed_nudge": wb_io_trim,
        # headline: WorkBoard (full nudge) vs Letta (memory-attributable, conservative)
        "wb_vs_letta_mem_pct": round((1 - wb_io_full / letta_mem_io) * 100, 1) if letta_mem_io else None,
        "wb_trim_vs_letta_mem_pct": round((1 - wb_io_trim / letta_mem_io) * 100, 1) if letta_mem_io else None,
        "wb_vs_letta_full_pct": round((1 - wb_io_full / letta_full_io) * 100, 1) if letta_full_io else None,
        "note": "WorkBoard full-nudge vs Letta memory-attributable is the "
                "conservative comparison; vs Letta's full in-context payload the "
                "gap is larger.",
    }

    # (3) WRITE dimension -----------------------------------------------------
    write = {
        "workboard_model_tokens_per_session": 0,
        "workboard_model_calls_per_session": 0,
        "letta_memory_tool_calls_per_session": live.get("write_tool_calls_per_session"),
        "letta_model_reported_total_per_session": live.get("model_reported_total_per_session"),
        "note": "WorkBoard persists via deterministic card.py (0 model tokens). "
                "Letta persists via LLM memory tool calls, and runs a Haiku "
                "compaction call when context fills (extra, not counted here).",
    }

    out = {
        "tokenizer": tokencount.backend_name(),
        "snapshot": safety.snapshot_fingerprint(),
        "letta_source": live.get("model"),       # 'real' or 'spec'
        "letta_backend": live.get("backend"),
        "letta_version": live.get("letta_version"),
        "scenario": {"turns": T, "sessions": N, "recalls_per_session": K},
        "per_turn_overhead": per_turn,
        "live_loop_projection": loop,
        "write_dimension": write,
        "per_recall": {"workboard": wb_recall, "letta": lt_recall,
                       "letta_model": lt.recall({"id": "a", "shape": "x"}).get("model")},
    }
    safety.assert_write_local(RESULTS)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "live_letta.json").write_text(json.dumps(out, indent=2))
    return out


def _print(o):
    pt = o["per_turn_overhead"]
    lp = o["live_loop_projection"]
    print(f"WorkBoard vs Letta — live loop   (Letta source: {o['letta_source']}, "
          f"backend {o['letta_backend']})\n")
    print("(1) PER-TURN memory overhead:")
    print(f"    Letta : {pt['letta_memory_attributable']} tok (blocks + memory-tool "
          f"schemas)  |  full in-context {pt['letta_full_incontext']} tok")
    print(f"    WorkBoard : {pt['workboard_nudge_full']} tok nudge "
          f"(trimmed {pt['workboard_nudge_trimmed']}); 0 memory carried in context\n")
    print(f"(2) LIVE LOOP — {lp['sessions']} sessions × {lp['turns']} turns × "
          f"{lp['recalls_per_session']} recalls:")
    print(f"    WorkBoard {lp['workboard_tokens_full_nudge']:,}  vs  "
          f"Letta {lp['letta_memory_attributable_tokens']:,} (memory-attributable)")
    print(f"    → WorkBoard {lp['wb_vs_letta_mem_pct']}% fewer model tokens "
          f"(trimmed nudge: {lp['wb_trim_vs_letta_mem_pct']}%)")
    print(f"    vs Letta full in-context {lp['letta_full_incontext_tokens']:,} "
          f"→ {lp['wb_vs_letta_full_pct']}% fewer")


if __name__ == "__main__":
    _print(run())
