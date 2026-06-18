"""graphify live-cost surface — MEASURED constants only (calibration.json).

graphify's measured Claude Code footprint (captured from a real sandboxed
install, see ../measure_graphify_real.md):
  - one CLAUDE.md trigger line .............. always-on, cached once/session
  - SKILL.md ................................ loaded when the skill engages
  - references/*.md ......................... loaded ON DEMAND only
  - `graphify query` BFS subgraph ........... per knowledge query
  - git post-commit rebuild ................. local tree-sitter AST, 0 API tokens

There is NO PreToolUse hook and NO per-prompt injection (verified: no
settings.json written). So graphify's per-prompt steady-state cost is 0.
"""

from __future__ import annotations
import json
from pathlib import Path

CAL = json.loads((Path(__file__).resolve().parent.parent /
                  "results" / "raw" / "calibration.json").read_text())
G = CAL["graphify"]


def live_cost(prompts: int, engagements: int = 1, recalls: int = 3,
              ref_loads: int = 0) -> dict:
    """One working session's live CONTEXT cost for graphify."""
    always_on = G["always_on_per_session_tok"]
    skill = G["skill_md_tok"] * engagements
    refs = round(G["references_total_tok"] / 8) * ref_loads      # avg one ref doc
    query = G["query_subgraph_mean_tok"] * recalls
    per_prompt = G["per_prompt_injection_tok"] * prompts          # = 0
    total = always_on + skill + refs + query + per_prompt
    return {
        "system": "graphify",
        "always_on": always_on,
        "per_prompt_total": per_prompt,
        "skill_load": skill,
        "reference_loads": refs,
        "recall_total": query,
        "write_tokens": G["write_api_tokens"],                    # 0 (local AST)
        "session_total": total,
        "captures": G["captures"],
    }


def ingest_note() -> str:
    return ("graphify code ingest is local tree-sitter AST — ~0 API tokens. "
            "Only docs/PDF ingest spends model tokens (configurable --token-budget). "
            "So bootstrap is NOT a WorkBoard win for code; stated honestly.")
