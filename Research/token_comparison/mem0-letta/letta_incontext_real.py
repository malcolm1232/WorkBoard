"""REAL Letta per-turn in-context measurement — server-independent.

Runs under .letta-venv (py3.13). Imports the INSTALLED Letta package and measures
the exact context a default Letta chat agent re-sends to the model EVERY turn,
using Letta's own shipped artifacts + context-assembly code:

  * system prompt   — letta.prompts.gpt_system.get_system_text("memgpt_v2_chat"),
                      the default modern chat-agent prompt (use_letta_v1_agent=False).
  * tool schemas    — generate_schema() over the REAL base tool set a chat agent
                      attaches: constants.BASE_TOOLS + BASE_MEMORY_TOOLS_V2.
  * memory blocks   — letta.schemas.memory.Memory(...).compile(), the real in-context
                      block rendering, for 2 default blocks (human + persona, 5000-char).

Every string is counted with tiktoken cl100k_base — the SAME tokenizer the rest of
the harness uses (the core fairness control). This is "real" in the strict sense:
Letta's actual prompts, actual tool JSON schemas, and actual block-compile output —
not a hand-guessed spec. It does NOT depend on a running server, a database, or the
backend model, so it reproduces anywhere `pip install letta` works.

(An end-to-end live run for model-reported usage is a separate, optional
corroboration — see letta_real_run.py / the docker path. The structural per-turn
cost measured here is the headline and is model-independent.)

Writes results/raw/letta_real.json (read by peers/letta_adapter.py).
"""

from __future__ import annotations
import json
from pathlib import Path

import tiktoken
import letta
import letta.constants as C
from letta.prompts import gpt_system
from letta.functions.schema_generator import generate_schema
from letta.functions.function_sets import base as B
from letta.schemas.memory import Memory
from letta.schemas.block import Block

BENCH = Path(__file__).resolve().parent
OUT = BENCH / "results" / "raw" / "letta_real.json"

ENC = tiktoken.get_encoding("cl100k_base")
def tk(s: str) -> int:
    return len(ENC.encode(s or "", disallowed_special=()))

# Tools whose presence is *because of* memory (the apples-to-apples surface vs
# WorkBoard's nudge). send_message is the only base tool that isn't memory plumbing.
MEMORY_TOOLNAMES = {
    "memory_replace", "memory_insert", "core_memory_append", "core_memory_replace",
    "archival_memory_insert", "archival_memory_search", "conversation_search",
}

# Default modern chat agent (use_letta_v1_agent=False -> memgpt_v2 family).
SYS_NAME = "memgpt_v2_chat"
TOOLNAMES = C.BASE_TOOLS + C.BASE_MEMORY_TOOLS_V2     # send_message, conversation_search,
                                                      # archival_*; memory_replace, memory_insert


def measure() -> dict:
    systxt = gpt_system.get_system_text(SYS_NAME)
    sys_tok = tk(systxt)

    tool_rows = {}
    tool_tok = mem_tool_tok = 0
    for n in TOOLNAMES:
        fn = getattr(B, n, None)
        if fn is None:
            continue
        t = tk(json.dumps(generate_schema(fn, name=n)))
        tool_rows[n] = t
        tool_tok += t
        if n in MEMORY_TOOLNAMES:
            mem_tool_tok += t

    def blocks(vh, vp):
        return [Block(label="human", value=vh, limit=5000),
                Block(label="persona", value=vp, limit=5000)]

    human = ("Name: the user. A software engineer working on the WorkBoard project.")
    persona = ("I am a helpful assistant that helps track and remember the user's "
               "engineering work as it happens.")
    cur = tk(Memory(blocks=blocks(human, persona)).compile())
    full = tk(Memory(blocks=blocks("x" * 5000, "y" * 5000)).compile())

    per_turn_current = sys_tok + tool_tok + cur
    per_turn_full = sys_tok + tool_tok + full
    # memory-attributable per-turn = blocks + memory-tool schemas (exclude base
    # send_message + the non-memory part of the system prompt — conservative).
    mem_attrib_current = cur + mem_tool_tok
    mem_attrib_full = full + mem_tool_tok

    return {
        "backend": "letta artifacts (server-independent); model-independent",
        "letta_version": letta.__version__,
        "measurement": "REAL — Letta's shipped system prompt + generated base/memory "
                       "tool JSON schemas + Memory.compile() block rendering, tiktoken cl100k.",
        "tokenizer_incontext": "tiktoken/cl100k_base (same as harness — fair)",
        "system_prompt": SYS_NAME,
        "tools_attached": list(tool_rows.keys()),
        "tool_schema_tokens_by_tool": tool_rows,
        "live": {
            # FAIR headline — deterministic per-turn in-context overhead
            "per_turn_incontext_tiktoken": per_turn_current,
            "per_turn_incontext_full_capacity": per_turn_full,
            "per_turn_memory_attributable": mem_attrib_current,
            "per_turn_memory_attributable_full": mem_attrib_full,
            "per_turn_incontext_breakdown": {
                "system": sys_tok,
                "blocks": cur,
                "blocks_full_capacity": full,
                "tool_schemas": tool_tok,
                "memory_tool_schemas": mem_tool_tok,
            },
            # writes: each persisted fact is one memory tool call (model output);
            # WorkBoard's deterministic card.py write is 0 model tokens.
            "writes_per_session_mean": None,   # measured only in the live model run
            "write_tool_tokens_per_session_mean": None,
            "model_reported_total_per_session_mean": None,
            "model_reported_total_per_turn_mean": None,
        },
        # recall: archival_memory_search call + injected results (spec-shaped; the
        # search tool schema is real, the result size uses Letta's archival token
        # limit as the cap).
        "recall": {
            "mean_search_call_tokens": tool_rows.get("archival_memory_search", 0),
            "mean_result_tokens": min(600, C.__dict__.get("RETRIEVAL_QUERY_DEFAULT_PAGE_SIZE", 0) or 600),
            "mean_total_tokens_per_recall": tool_rows.get("archival_memory_search", 0) + 600,
            "note": "archival_memory_search schema is real; result bundle capped at "
                    "~600 tok (Letta archival_memory_token_limit is 8192 — generous "
                    "to Letta to cap low).",
        },
    }


def main():
    out = measure()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    lv = out["live"]
    print(f"letta {out['letta_version']} — REAL per-turn in-context "
          f"(tiktoken cl100k):")
    print(f"  current  : {lv['per_turn_incontext_tiktoken']} tok  "
          f"(system {lv['per_turn_incontext_breakdown']['system']}, "
          f"tools {lv['per_turn_incontext_breakdown']['tool_schemas']}, "
          f"blocks {lv['per_turn_incontext_breakdown']['blocks']})")
    print(f"  full-cap : {lv['per_turn_incontext_full_capacity']} tok")
    print(f"  memory-attributable: {lv['per_turn_memory_attributable']} tok "
          f"(blocks + memory-tool schemas)")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
