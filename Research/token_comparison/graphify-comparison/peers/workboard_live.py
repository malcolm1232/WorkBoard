"""WorkBoard live-cost surface — constants from calibration.json.

Measured on the same frozen board snapshot with the same tokenizer:
  - SessionStart digest .......... once per session
  - UserPromptSubmit nudge ....... EVERY prompt (306 tok; trimmable to 40).
                                   This is the lever that makes carding a
                                   0-model-token deterministic write.
  - SKILL.md ..................... loaded when the board engages
  - card.py show/list recall ..... per work-outcome query (reused from the
                                   claude-mem study, same snapshot)
  - carding write ................ 0 model tokens (deterministic CLI)
"""

from __future__ import annotations
import json
from pathlib import Path

CAL = json.loads((Path(__file__).resolve().parent.parent /
                  "results" / "raw" / "calibration.json").read_text())
W = CAL["workboard"]


def live_cost(prompts: int, engagements: int = 1, recalls: int = 3,
              trimmed: bool = False) -> dict:
    nudge = W["per_prompt_nudge_trimmed_tok"] if trimmed else W["per_prompt_nudge_tok"]
    always_on = W["always_on_per_session_tok"]
    per_prompt = nudge * prompts
    skill = W["skill_md_tok"] * engagements
    recall = W["recall_mean_tok"] * recalls
    total = always_on + per_prompt + skill + recall
    return {
        "system": "workboard" + ("-trimmed" if trimmed else ""),
        "always_on": always_on,
        "per_prompt_total": per_prompt,
        "per_prompt_each": nudge,
        "skill_load": skill,
        "reference_loads": 0,
        "recall_total": recall,
        "write_tokens": W["write_model_tokens"],                  # 0 (deterministic)
        "session_total": total,
        "captures": W["captures"],
    }
