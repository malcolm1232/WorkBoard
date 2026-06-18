"""Letta adapter for the letta-comparison study.

Unlike the mem0 / claude-mem peers (modeled from each vendor's own published
numbers), Letta is measured by a **REAL local run** — the user asked for it, and
Letta is the one peer cheap enough to run truly locally (Ollama backend, no API
key, no cost). The heavy real run lives in `letta_real_run.py` (a separate
Python-3.13 process, because Letta needs py>=3.11 while this harness runs on the
system py3.9). That process writes `results/raw/letta_real.json`; THIS adapter
just reads it — exactly the read-a-result-file shape the other peers use.

If `letta_real.json` is absent (Letta not installed / run skipped), the adapter
falls back to a SPEC model derived from Letta's own published design, so the
harness still renders. Every value is tagged `model: "real"` or `model: "spec"`
so the report never blurs the two.

WHY LETTA'S COST SHAPE IS DIFFERENT (and why we measure per-TURN, not per-session)
---------------------------------------------------------------------------------
mem0 and claude-mem pay a once-per-session extraction/compression call. Letta —
the canonical "LLM manages its own memory" (MemGPT) system — pays CONTINUOUSLY:

  * Its core memory blocks (default 2: `human` + `persona`, 5000 chars each) are
    "always visible … prepended to the agent's prompt every interaction"
    (docs.letta.com/guides/agents/memory-blocks). So they cost tokens EVERY TURN.
  * Every memory write is an LLM **tool call** (`core_memory_append/replace`,
    `memory_insert/replace`, `archival_memory_insert`) — input = the in-context
    state the model reads to decide + emit the call, output = the tool args.
  * When context fills, Letta runs a **compaction** LLM call (Haiku by default;
    summarizes ~30% of messages) — docs.letta.com/.../messages/compaction.

So Letta's dominant tax is PER-TURN (in-context blocks + tool schemas), which is
the fair surface to compare against WorkBoard's per-turn protocol nudge. WorkBoard
persists via a deterministic `card.py` write — 0 model tokens, no in-context
memory carried turn to turn.

FAIRNESS / TOKENIZER NOTE
-------------------------
The rest of the harness counts every system with one tokenizer (tiktoken
cl100k) — the core fairness control. Letta's *model-reported* usage uses the
llama tokenizer, so for the head-to-head we use the **tiktoken** count of the
exact context Letta assembles each turn (system prompt + memory blocks + tool
schemas), captured from the live agent. Letta's raw model-reported totals are
reported alongside as real-world corroboration, clearly labelled.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))
import tokencount  # noqa: E402  (kept for symmetry / parity with peers)

REAL_PATH = BENCH_DIR / "results" / "raw" / "letta_real.json"


# --- Letta published design (SPEC fallback only; real run overrides all) ------
# Sources: docs.letta.com guides — memory-blocks (5000-char default blocks,
# always in-context), memory (built-in memory tools), messages/compaction.
DEFAULTS = {
    "n_core_blocks": 2,                 # human + persona (default chat agent)
    "block_char_limit": 5000,           # default per-block limit
    "block_fill_frac": 0.5,             # blocks rarely full; assume ~half (generous to Letta)
    "chars_per_token": 4,               # rough English ratio for the spec estimate
    "tool_schema_tokens": 350,          # memory tool JSON schemas carried in context
    "system_prompt_tokens": 320,        # Letta base system / MemGPT instructions
    "write_tool_call_tokens": 60,       # output tokens for one memory tool call (args)
    "writes_per_session": 3,            # memory edits a working session triggers
    "recall_tool_call_tokens": 40,      # archival_memory_search call args
    "recall_result_tokens": 600,        # fetched archival results injected back
}


def _spec_per_turn_incontext() -> int:
    p = DEFAULTS
    block_tok = (p["n_core_blocks"] * p["block_char_limit"] * p["block_fill_frac"]
                 / p["chars_per_token"])
    return int(round(block_tok + p["tool_schema_tokens"] + p["system_prompt_tokens"]))


def _load_real() -> dict | None:
    if REAL_PATH.exists():
        try:
            return json.loads(REAL_PATH.read_text())
        except Exception:
            return None
    return None


def live(session_stats: dict | None = None, params: dict | None = None) -> dict:
    """Per-turn + per-session live cost of running Letta as you work.

    Real run (preferred): values come from letta_real.json — the tiktoken count
    of the context Letta actually assembled each turn (system + blocks + tool
    schemas), the measured memory-write tool-call output, and the model-reported
    raw totals for corroboration.

    Spec fallback: modeled from Letta's published defaults.
    """
    real = _load_real()
    if real and real.get("live"):
        lv = real["live"]
        return {
            "system": "letta",
            "model": "real",
            "backend": real.get("backend"),
            "letta_version": real.get("letta_version"),
            "sessions_run": real.get("sessions_run"),
            # FAIR (tiktoken) per-turn in-context memory overhead — the headline surface
            "per_turn_incontext_tokens": lv["per_turn_incontext_tiktoken"],
            "per_turn_incontext_breakdown": lv.get("per_turn_incontext_breakdown"),
            # write = output tokens spent emitting memory tool calls, per session
            "write_tool_calls_per_session": lv.get("writes_per_session_mean"),
            "write_tool_call_tokens_per_session": lv.get("write_tool_tokens_per_session_mean"),
            # corroboration: Letta's own model-reported usage (llama tokenizer)
            "model_reported_total_per_session": lv.get("model_reported_total_per_session_mean"),
            "model_reported_total_per_turn": lv.get("model_reported_total_per_turn_mean"),
            "note": "Letta carries memory in-context EVERY turn; tax is per-turn, "
                    "not once-per-session like mem0/claude-mem.",
        }
    # ---- spec fallback ----
    pt = _spec_per_turn_incontext()
    p = {**DEFAULTS, **(params or {})}
    return {
        "system": "letta",
        "model": "spec",
        "backend": None,
        "per_turn_incontext_tokens": pt,
        "per_turn_incontext_breakdown": {
            "blocks": int(round(p["n_core_blocks"] * p["block_char_limit"]
                                * p["block_fill_frac"] / p["chars_per_token"])),
            "tool_schemas": p["tool_schema_tokens"],
            "system": p["system_prompt_tokens"],
        },
        "write_tool_calls_per_session": p["writes_per_session"],
        "write_tool_call_tokens_per_session": p["writes_per_session"] * p["write_tool_call_tokens"],
        "model_reported_total_per_session": None,
        "model_reported_total_per_turn": None,
        "note": "SPEC fallback (letta_real.json absent) — Letta's published defaults.",
    }


def recall(query: dict, n_units: int = 1, params: dict | None = None) -> dict:
    """Tokens Letta spends to answer one recall: an archival_memory_search tool
    call (args) + the fetched results injected back into context. Real if the run
    measured it, else spec."""
    real = _load_real()
    if real and real.get("recall"):
        rc = real["recall"]
        total = rc.get("mean_total_tokens_per_recall")
        return {
            "system": "letta", "model": "real",
            "id": query["id"], "shape": query["shape"],
            "index_tokens": rc.get("mean_search_call_tokens"),
            "detail_tokens": rc.get("mean_result_tokens"),
            "detail_units": n_units,
            "total_tokens": total,
        }
    p = {**DEFAULTS, **(params or {})}
    total = p["recall_tool_call_tokens"] + p["recall_result_tokens"]
    return {
        "system": "letta", "model": "spec",
        "id": query["id"], "shape": query["shape"],
        "index_tokens": p["recall_tool_call_tokens"],
        "detail_tokens": p["recall_result_tokens"],
        "detail_units": n_units,
        "total_tokens": total,
    }


if __name__ == "__main__":
    print(json.dumps(live({}), indent=2))
    print(json.dumps(recall({"id": "P05", "shape": "pinpoint"}), indent=2))
