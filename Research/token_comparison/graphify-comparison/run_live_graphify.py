"""LIVE STUDY — WorkBoard vs graphify (graphifyy 0.8.41), measured head-to-head.

Composes MEASURED constants from results/raw/calibration.json into per-session
live-context scenarios + a per-prompt sensitivity sweep. Writes
results/raw/live.json. Reads only frozen files; all writes stay in this folder.

HONEST FRAMING (the data forced it): graphify is a LIGHTWEIGHT peer, not a
heavyweight. There is no 95%-style win — writes tie at 0 tokens, graphify is
lighter per-prompt (it injects nothing), WorkBoard is lighter on SKILL.md. The
real difference is SHAPE: work outcomes vs code structure, at similar low cost.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH / "lib"))
import safety                              # noqa: E402
from peers import graphify_live as gf      # noqa: E402
from peers import workboard_live as wb      # noqa: E402

RESULTS = BENCH / "results" / "raw"


def _pct(a, b):
    return round((1 - a / b) * 100, 1) if b else 0.0


def run(prompts: int = 50, engagements: int = 1, recalls: int = 3) -> dict:
    safety.assert_non_invasive()

    g = gf.live_cost(prompts, engagements, recalls)
    w = wb.live_cost(prompts, engagements, recalls, trimmed=False)
    wt = wb.live_cost(prompts, engagements, recalls, trimmed=True)

    sweep = []
    for T in (1, 10, 50, 100):
        sweep.append({
            "prompts": T,
            "graphify": gf.live_cost(T, 0, 0)["per_prompt_total"],
            "workboard_full": wb.live_cost(T, 0, 0)["per_prompt_total"],
            "workboard_trimmed": wb.live_cost(T, 0, 0, trimmed=True)["per_prompt_total"],
        })

    skill_delta = _pct(wb.CAL["workboard"]["skill_md_tok"],
                       gf.CAL["graphify"]["skill_md_tok"])

    out = {
        "tokenizer": gf.CAL["_meta"]["tokenizer"],
        "graphify_version": gf.CAL["_meta"]["graphify_version"],
        "snapshot": safety.snapshot_fingerprint(),
        "integration_measured": gf.CAL["_meta"]["graphify_integration_measured"],
        "graph": gf.CAL["_meta"]["graph"],
        "scenario": {"prompts": prompts, "engagements": engagements, "recalls": recalls},
        "session_totals": {"graphify": g, "workboard": w, "workboard_trimmed": wt},
        "component_winners": {
            "always_on_per_prompt": "graphify — injects 0/prompt; WorkBoard pays the nudge",
            "skill_on_engagement": f"WorkBoard — {wb.CAL['workboard']['skill_md_tok']} vs "
                                   f"{gf.CAL['graphify']['skill_md_tok']} tok "
                                   f"({skill_delta}% lighter), before graphify pulls references",
            "per_recall": "different questions (work outcomes vs code structure) — not head-to-head",
            "write": "TIE — both 0 tokens (card.py vs local AST); different artifacts",
            "big_artifact_autoload": "TIE — neither auto-loads (board.json / graph.json on disk)",
        },
        "skill_md_workboard_lighter_pct": skill_delta,
        "per_prompt_sweep": sweep,
        "ingest_note": gf.ingest_note(),
        "headline": (
            "graphify is a lightweight peer. No 95%-style efficiency win exists vs "
            "graphify (unlike vs claude-mem/mem0/Letta, which pay a per-session or "
            "per-turn LLM cost). Difference is shape — work outcomes vs code "
            "structure — at similar low cost. WorkBoard's one heavier surface is the "
            "per-prompt nudge (306 tok, trimmable to 40); graphify injects nothing "
            "per prompt."
        ),
    }
    safety.assert_write_local(RESULTS)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "live.json").write_text(json.dumps(out, indent=2))
    return out


def _print(o):
    s = o["session_totals"]
    print(f"LIVE — WorkBoard vs graphify ({o['graphify_version']})")
    print(f"snapshot {o['snapshot'].get('sha256','?')} · tokenizer {o['tokenizer'].split('(')[0].strip()}")
    print(f"scenario {o['scenario']}\n")
    print(f"{'component':<22}{'graphify':>12}{'workboard':>12}{'wb-trimmed':>12}")
    for key, lbl in [("always_on", "always-on/sess"), ("per_prompt_total", "per-prompt (all T)"),
                     ("skill_load", "SKILL on engage"), ("recall_total", "recall total*"),
                     ("write_tokens", "write"), ("session_total", "SESSION TOTAL")]:
        print(f"{lbl:<22}{s['graphify'][key]:>12}{s['workboard'][key]:>12}{s['workboard_trimmed'][key]:>12}")
    print(f"\nSKILL.md: WorkBoard {o['skill_md_workboard_lighter_pct']}% lighter than graphify")
    print("\nper-prompt always-on sweep (graphify injects 0):")
    for r in o["per_prompt_sweep"]:
        print(f"  T={r['prompts']:>3}: graphify {r['graphify']:>5}  "
              f"WorkBoard {r['workboard_full']:>6}  (trimmed {r['workboard_trimmed']:>5})")
    print("\n* recall mixes different question types — not a head-to-head.")
    print(f"\n{o['headline']}")


if __name__ == "__main__":
    _print(run())
