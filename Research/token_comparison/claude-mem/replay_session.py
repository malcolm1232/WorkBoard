"""STUDY C — Live carding (steady-state cost of HAVING the system on as you work).

Two cost dimensions per working session, measured the same way for both systems:

  (1) MEMORY-WRITE  — model calls + tokens spent to PERSIST the session's work.
      WorkBoard : carding is inline `card.py add/fly` — deterministic CLI; the
                  writeup is produced by the main model as part of its normal
                  turn output. → 0 extra model calls, 0 extra model-input tokens.
      claude-mem: one SessionEnd Agent-SDK compression call per session, reading
                  the full transcript. → 1 call, ~avg-session-transcript tokens.

  (2) CONTEXT-INJECTION — interactive tokens the system adds to the session.
      WorkBoard : SessionStart digest (measured) + per-turn protocol nudge
                  (measured). The nudge is WorkBoard's one area that is NOT
                  lighter per-turn — it's a fixed reminder, trimmable to ~40 tok,
                  and it's the lever that makes (1) free.
      claude-mem: SessionStart memory injection (cited, grows with memory); its
                  per-turn hook injection is configurable — we don't fabricate it.

The honest story: WorkBoard MOVES the memory-write cost off the model entirely
(inline deterministic carding), paying instead a small, fixed, trimmable per-turn
reminder. claude-mem keeps the session quiet but pays a full compression call
every single session — the bootstrap cost, paid forever.

WorkBoard numbers are MEASURED here; claude-mem write cost is derived from the
SAME corpus (avg transcript tokens/session); claude-mem injection is cited from
docs/TOKEN_BUDGET.md. Writes results/raw/live.json.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))
import tokencount                    # noqa: E402
import run_recall                    # noqa: E402

RESULTS = BENCH_DIR / "results" / "raw"

# --- MEASURED WorkBoard live injections (this machine, frozen snapshot) -------
WB_SESSIONSTART_DIGEST = 97     # `card.py --board <snapshot> digest`
WB_PER_TURN_NUDGE = 306         # scripts/hook_user_prompt.sh protocol nudge
WB_NUDGE_TRIMMED = 40           # TOKEN_BUDGET.md: trimmable to a one-liner
WB_WRITE_MODEL_CALLS = 0        # carding is inline CLI — no extra model call
WB_WRITE_MODEL_TOKENS = 0

# --- CITED peer live numbers (docs/TOKEN_BUDGET.md, vetted) -------------------
CM_SESSIONSTART_CITED = "~thousands (grows with stored memory)"
MEM0_RECALL_CITED = 6719


def _avg_session_transcript_tokens() -> int:
    """claude-mem's SessionEnd compression reads the whole transcript; the avg
    session size IS its per-session write-input. Pull from the bootstrap run
    (same corpus, already measured)."""
    boot = json.loads((RESULTS / "bootstrap.json").read_text())
    med = next((f for f in boot["fixtures"] if f["corpus"] == "medium"),
               boot["fixtures"][0])
    return round(med["cm_ingest_input_tokens"] / med["sessions"])


def run(turns: int = 50, sessions_projection: int = 100) -> dict:
    rec = run_recall.run()
    found = [r for r in rec["rows"] if r["wb_found"]]
    wb_recall_avg = round(sum(r["wb_total"] for r in found) / len(found))
    cm_recall_avg = round(sum(r["cm_total"] for r in found) / len(found))

    cm_write_in = _avg_session_transcript_tokens()

    # per-session memory-WRITE (the clean head-to-head dimension)
    write = {
        "workboard_model_calls": WB_WRITE_MODEL_CALLS,
        "workboard_model_tokens": WB_WRITE_MODEL_TOKENS,
        "claude_mem_model_calls": 1,
        "claude_mem_model_input_tokens": cm_write_in,
        "note": "WorkBoard cards inline (deterministic CLI); claude-mem runs 1 "
                "SessionEnd compression call/session reading the full transcript.",
    }
    # projected over a project lifetime
    projection = {
        "sessions": sessions_projection,
        "workboard_write_calls": 0,
        "workboard_write_tokens": 0,
        "claude_mem_write_calls": sessions_projection,
        "claude_mem_write_tokens": cm_write_in * sessions_projection,
    }
    # per-session context injection
    inject = {
        "workboard_sessionstart": WB_SESSIONSTART_DIGEST,
        "workboard_per_turn_nudge": WB_PER_TURN_NUDGE,
        "workboard_per_turn_nudge_trimmed": WB_NUDGE_TRIMMED,
        "workboard_session_inject_full": WB_SESSIONSTART_DIGEST + WB_PER_TURN_NUDGE * turns,
        "workboard_session_inject_trimmed": WB_SESSIONSTART_DIGEST + WB_NUDGE_TRIMMED * turns,
        "workboard_board_autoload": 0,
        "workboard_scales_with_memory_size": False,
        "claude_mem_sessionstart": CM_SESSIONSTART_CITED,
        "claude_mem_scales_with_memory_size": True,
    }

    out = {
        "tokenizer": tokencount.backend_name(),
        "scenario": {"turns": turns},
        "memory_write_per_session": write,
        "memory_write_projection": projection,
        "context_injection_per_session": inject,
        "per_recall": {
            "workboard_measured": wb_recall_avg,
            "claude_mem_spec_bestcase": cm_recall_avg,
            "mem0_cited": MEM0_RECALL_CITED,
            "wb_vs_cm_pct": round((1 - wb_recall_avg / cm_recall_avg) * 100, 1),
            "wb_vs_mem0_pct": round((1 - wb_recall_avg / MEM0_RECALL_CITED) * 100, 1),
        },
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "live.json").write_text(json.dumps(out, indent=2))
    return out


def _print(o):
    w = o["memory_write_per_session"]
    p = o["memory_write_projection"]
    i = o["context_injection_per_session"]
    print("STUDY C — Live carding\n")
    print("(1) MEMORY-WRITE per session (persisting the work):")
    print(f"    WorkBoard : {w['workboard_model_calls']} model calls, "
          f"{w['workboard_model_tokens']} tokens  (inline carding)")
    print(f"    claude-mem: {w['claude_mem_model_calls']} model call, "
          f"{w['claude_mem_model_input_tokens']:,} input tokens  (SessionEnd compression)")
    print(f"    → over {p['sessions']} sessions: WorkBoard {p['workboard_write_tokens']:,} vs "
          f"claude-mem {p['claude_mem_write_tokens']:,} tokens\n")
    print("(2) CONTEXT-INJECTION per session:")
    print(f"    WorkBoard : {i['workboard_sessionstart']} (SessionStart) + "
          f"{i['workboard_per_turn_nudge']}/turn nudge "
          f"= {i['workboard_session_inject_full']:,}/{o['scenario']['turns']}-turn "
          f"(trimmable to {i['workboard_session_inject_trimmed']:,}); board autoload = 0")
    print(f"    claude-mem: {i['claude_mem_sessionstart']}\n")
    pr = o["per_recall"]
    print("(3) PER-RECALL (from Study B):")
    print(f"    WorkBoard {pr['workboard_measured']} vs claude-mem "
          f"{pr['claude_mem_spec_bestcase']} ({pr['wb_vs_cm_pct']}% lighter) "
          f"vs mem0 {pr['mem0_cited']} ({pr['wb_vs_mem0_pct']}% lighter)")


if __name__ == "__main__":
    _print(run())
