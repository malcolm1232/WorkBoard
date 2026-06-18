"""Render REPORT.md (concise) and REPORT_DETAILED.md from results/raw/*.json.

Deterministic, offline. Writes only inside this study folder.
Run: python3 run_live_graphify.py && python3 render_report.py
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH / "lib"))
import safety  # noqa: E402

RAW = BENCH / "results" / "raw"


def _load():
    live = json.loads((RAW / "live.json").read_text())
    cal = json.loads((RAW / "calibration.json").read_text())
    return live, cal


def render_concise(live, cal) -> str:
    g, w, wt = (live["session_totals"][k] for k in ("graphify", "workboard", "workboard_trimmed"))
    G, W = cal["graphify"], cal["workboard"]
    sc = live["scenario"]
    L = []; a = L.append
    a("# WorkBoard vs graphify — live operating cost (measured)\n")
    a(f"> {live['graphify_version']} · tokenizer `{live['tokenizer'].split('(')[0].strip()}` "
      f"(identical for both = fairness) · board snapshot `{live['snapshot'].get('sha256','?')}`\n")
    a("**Headline.** " + live["headline"] + "\n")
    a("## What graphify actually is (measured, not assumed)\n")
    a("graphify is a **code-structure knowledge graph** (tree-sitter AST → "
      f"`graph.json`: {live['graph']['nodes']} nodes / {live['graph']['edges']} edges on "
      f"{cal['_meta']['code_corpus']}). Its Claude Code integration, captured from a real "
      "sandboxed install, is:\n")
    a(f"- {live['integration_measured']}\n")
    a("This contradicts the rendered-docs claim of a *PreToolUse-fires-on-every-read* hook — "
      "the installed tool writes **no `settings.json` and no hook entry**. graphify's "
      "per-prompt steady-state cost is **0**; its weight sits in the on-engagement SKILL "
      "load and the per-query subgraph.\n")
    a("## Measured live-context footprint (tiktoken cl100k)\n")
    a("| Surface | WorkBoard | graphify | Lighter |")
    a("|---|--:|--:|---|")
    a(f"| Always-on / **prompt** | {W['per_prompt_nudge_tok']} nudge (+{W['always_on_per_session_tok']} digest/sess) | "
      f"{G['always_on_per_session_tok']} once, cached | **graphify** |")
    a(f"| SKILL.md (on engage) | **{W['skill_md_tok']}** | {G['skill_md_tok']} (+{G['references_total_tok']} refs on demand) | "
      f"**WorkBoard** ({live['skill_md_workboard_lighter_pct']}%) |")
    a(f"| Per recall | {W['recall_mean_tok']} (work Qs) | {G['query_subgraph_mean_tok']} (code Qs) | different questions* |")
    a(f"| Write / keep current | 0 (card.py) | 0 (local AST) | **tie** |")
    a(f"| Big artifact autoload | board.json never | graph.json never | **tie** |")
    a("\n\\* WorkBoard recall answers *what shipped / why / links*; graphify recall answers "
      "*what calls what*. Token counts price different questions — not a head-to-head.\n")
    a("## The per-prompt nudge — WorkBoard's one heavier surface\n")
    a("WorkBoard injects a protocol nudge **every prompt**; that lever makes carding a "
      "**0-model-token deterministic write**. graphify injects nothing per prompt. Over a "
      "session this is the whole steady-state gap:\n")
    a("| Prompts | graphify | WorkBoard (306) | WorkBoard trimmed (40) |")
    a("|--:|--:|--:|--:|")
    for r in live["per_prompt_sweep"]:
        a(f"| {r['prompts']} | {r['graphify']} | {r['workboard_full']} | {r['workboard_trimmed']} |")
    a("\nTrimming the nudge to ~40 tok (already a `TOKEN_BUDGET.md` lever) closes most of the "
      "gap. **Recommendation: trim the nudge.** This study makes no product change.\n")
    a(f"## Illustrative session total ({sc['prompts']} prompts, {sc['engagements']} skill load, "
      f"{sc['recalls']} recalls)\n")
    a("| | graphify | WorkBoard | WorkBoard trimmed |")
    a("|---|--:|--:|--:|")
    for key, lbl in [("always_on", "always-on"), ("per_prompt_total", "per-prompt"),
                     ("skill_load", "SKILL load"), ("recall_total", "recall*"),
                     ("write_tokens", "write"), ("session_total", "**total**")]:
        a(f"| {lbl} | {g[key]} | {w[key]} | {wt[key]} |")
    a("\n\\* recall mixes question types; shown for completeness. Honest read: with the full "
      "nudge WorkBoard is heavier in steady state; trimmed it is comparable; graphify is "
      "genuinely light.\n")
    a("## Bootstrap (kept light, honest)\n")
    a(live["ingest_note"] + "\n")
    a("## Conclusion\n")
    a("Against **claude-mem / mem0 / Letta**, WorkBoard wins the live loop because those "
      "tools pay an LLM cost per session (claude-mem/mem0) or per turn (Letta). Against "
      "**graphify** that lever does not exist — graphify's writes are local AST at 0 API "
      "tokens too. Honest verdict: **different shape, similar low cost.** WorkBoard records "
      "*work outcomes*; graphify records *code structure*. Complements, not competitors.\n")
    a("> Reproduce: `python3 run_live_graphify.py && python3 render_report.py`. "
      "Re-measure graphify from scratch: see `measure_graphify_real.md`. Full story: `CONTEXT.md`.\n")
    return "\n".join(L)


def render_detailed(live, cal) -> str:
    g, w, wt = (live["session_totals"][k] for k in ("graphify", "workboard", "workboard_trimmed"))
    G, W = cal["graphify"], cal["workboard"]
    m = cal["_meta"]; sc = live["scenario"]
    L = []; a = L.append
    a("# WorkBoard vs graphify — DETAILED report\n")
    a(f"> {m['graphify_version']} · measured {m['measured_on']} · tokenizer "
      f"`tiktoken-cl100k_base` (one tokenizer for both = fairness) · board snapshot "
      f"`{live['snapshot'].get('sha256','?')}` ({live['snapshot'].get('bytes','?')} B)\n")

    a("## 0. TL;DR\n")
    a(live["headline"] + "\n")
    a("There is **no defensible 95%-style efficiency headline vs graphify** — and that is "
      "itself the finding. claude-mem/mem0/Letta lose the live loop because they spend LLM "
      "tokens to *write* memory (per session or per turn). graphify does not: its graph is "
      "built by local tree-sitter AST at **0 API tokens**, the same order as WorkBoard's "
      "deterministic `card.py` write. The two systems are close on raw live tokens and "
      "differ mainly in **what they remember**.\n")

    a("## 1. Why graphify is a different shape than the other peers\n")
    a("| | claude-mem / mem0 / Letta | graphify | WorkBoard |")
    a("|---|---|---|---|")
    a("| Stores | conversation memory (vectors) | **code structure** (AST graph) | **work outcomes** (kanban cards) |")
    a("| Write path | LLM extraction/compression | local tree-sitter AST | deterministic `card.py` |")
    a("| Write token cost | thousands/session or /turn | **0 API** | **0 model** |")
    a("| Answers | 'what did we discuss' | 'what calls what' | 'what shipped / why / links' |")
    a("\nBecause graphify and WorkBoard both move the write off the model, the live-loop "
      "argument that beat claude-mem/mem0/Letta **does not apply to graphify**. The honest "
      "comparison is the *context footprint* of having each on, plus a frank statement that "
      "they answer different questions.\n")

    a("## 2. What graphify installs (measured from a real sandboxed run)\n")
    a(f"`graphifyy {m['graphify_version'].split()[-1]}` `graphify install --platform claude`, "
      "run under a throwaway `$HOME`, writes exactly:\n")
    a("```")
    a("$HOME/.claude/CLAUDE.md                     <- 1 trigger line (always-on, cached)")
    a("$HOME/.claude/skills/graphify/SKILL.md      <- loaded when skill engages")
    a("$HOME/.claude/skills/graphify/references/*  <- loaded ON DEMAND only")
    a("(no settings.json, no hooks entry)          <- => 0 per-prompt injection")
    a("```")
    a("Plus an **optional** `graphify hook install` = a *git post-commit* hook that rebuilds "
      "the graph locally on commit (tree-sitter AST, no LLM). So there is **no per-tool-use "
      "PreToolUse hook** — the widely-quoted 'fires before every file read' description does "
      "not match the installed artifact.\n")

    a("## 3. Every measured number (tiktoken cl100k)\n")
    a("### graphify\n")
    a("| Constant | Tokens | Loaded |")
    a("|---|--:|---|")
    a(f"| CLAUDE.md trigger line | {G['always_on_per_session_tok']} | always-on (cached once/session) |")
    a(f"| SKILL.md | {G['skill_md_tok']} | on engagement |")
    a(f"| references/*.md (total) | {G['references_total_tok']} | on demand only |")
    a(f"| query subgraph (mean of {len(G['query_samples_tok'])}) | {G['query_subgraph_mean_tok']} | per `graphify query` |")
    a(f"| &nbsp;&nbsp;query samples | {', '.join(map(str, G['query_samples_tok']))} | (BFS depth-2, code Qs) |")
    a(f"| write (code) | {G['write_api_tokens']} | local AST, no LLM |")
    a(f"| per-prompt injection | {G['per_prompt_injection_tok']} | none installed |")
    a(f"\nGraph built on {m['code_corpus']}: **{m['graph']['nodes']} nodes / "
      f"{m['graph']['edges']} edges**, `graph.json` = {m['graph']['graph_json_bytes']:,} B "
      "(never auto-loaded; queries pull a subgraph).\n")
    a("### WorkBoard\n")
    a("| Constant | Tokens | Loaded |")
    a("|---|--:|---|")
    a(f"| SessionStart digest | {W['always_on_per_session_tok']} | once/session |")
    a(f"| UserPromptSubmit nudge | {W['per_prompt_nudge_tok']} | **every prompt** |")
    a(f"| &nbsp;&nbsp;nudge trimmed | {W['per_prompt_nudge_trimmed_tok']} | trim lever (TOKEN_BUDGET.md) |")
    a(f"| SKILL.md | {W['skill_md_tok']} | on engagement |")
    a(f"| recall (mean) | {W['recall_mean_tok']} | per work query — {W['recall_source']} |")
    a(f"| write | {W['write_model_tokens']} | deterministic `card.py` |")

    a("\n## 4. Component-by-component verdict\n")
    a("| Axis | WorkBoard | graphify | Winner & why |")
    a("|---|--:|--:|---|")
    cw = live["component_winners"]
    a(f"| Always-on / prompt | {W['per_prompt_nudge_tok']} | {G['always_on_per_session_tok']} (cached) | "
      "**graphify** — it injects nothing per prompt |")
    a(f"| SKILL.md on engage | {W['skill_md_tok']} | {G['skill_md_tok']} | "
      f"**WorkBoard** ({live['skill_md_workboard_lighter_pct']}% lighter; graphify can pull +{G['references_total_tok']} refs) |")
    a(f"| Per recall | {W['recall_mean_tok']} | {G['query_subgraph_mean_tok']} | "
      "different questions — not a head-to-head |")
    a(f"| Write / keep current | 0 | 0 | **tie** — different artifacts |")
    a(f"| Big artifact autoload | 0 | 0 | **tie** — both on disk |")

    a("\n## 5. The per-prompt nudge is the whole steady-state gap\n")
    a("graphify adds 0 tokens per prompt; WorkBoard adds the nudge. Across a session that "
      "single difference dominates everything else:\n")
    a("| Prompts (T) | graphify | WorkBoard (306) | WorkBoard trimmed (40) |")
    a("|--:|--:|--:|--:|")
    for r in live["per_prompt_sweep"]:
        a(f"| {r['prompts']} | {r['graphify']} | {r['workboard_full']} | {r['workboard_trimmed']} |")
    a("\nThe nudge is **not free overhead** — it is the mechanism that makes WorkBoard's "
      "write cost 0 and keeps the board live. But it is trimmable to ~40 tok with no loss of "
      "the carding contract. **Recommendation: trim it** (turns a per-prompt liability into "
      "a rounding error).\n")

    a(f"## 6. Illustrative full session ({sc['prompts']} prompts, {sc['engagements']} skill "
      f"load, {sc['recalls']} recalls)\n")
    a("| Component | graphify | WorkBoard | WorkBoard trimmed |")
    a("|---|--:|--:|--:|")
    for key, lbl in [("always_on", "always-on"), ("per_prompt_total", "per-prompt"),
                     ("skill_load", "SKILL load"), ("recall_total", "recall (diff. Qs)"),
                     ("write_tokens", "write"), ("session_total", "**TOTAL**")]:
        a(f"| {lbl} | {g[key]} | {w[key]} | {wt[key]} |")
    a("\nRead it honestly: at full nudge WorkBoard's session total is **higher** than "
      "graphify's; trimmed, they are comparable. Neither is dramatically cheaper. The recall "
      "row mixes question types and should not be read as a head-to-head.\n")

    a("## 7. Bootstrap (intentionally light)\n")
    a(live["ingest_note"] + " WorkBoard's bootstrap (mining past sessions into cards) is a "
      "one-time Haiku cost documented in the claude-mem study; it is not re-litigated here "
      "because graphify's code ingest is a different operation (parsing source, not "
      "summarizing history).\n")

    a("## 8. Fairness controls\n")
    a("- **One tokenizer** (tiktoken cl100k) for every count, both systems.\n")
    a("- **graphify measured, not invented** — real install, real graph, real queries; where "
      "a number could go either way it was taken in graphify's favor (e.g. references "
      "counted as on-demand, not always-on).\n")
    a("- **Shape difference stated up front** — no token 'win' is claimed on questions "
      "graphify cannot answer, and graphify's leaner per-recall is reported plainly.\n")
    a("- **Product untouched** — graphify ran under a sandbox `$HOME`; the harness only reads "
      "frozen copies and writes inside this folder (`lib/safety.py` enforces it).\n")

    a("## 9. Conclusion\n")
    a("graphify is the one peer in this suite that is **not** beaten on live tokens, because "
      "it shares WorkBoard's core efficiency move (keep the big artifact on disk; write "
      "without the model). The right public statement is **not** 'WorkBoard is X% cheaper "
      "than graphify' — it is *'WorkBoard and graphify are both lightweight, on-disk, "
      "query-on-demand systems that remember different things: graphify maps your code, "
      "WorkBoard maps your work.'* Save the efficiency headlines for claude-mem / mem0 / "
      "Letta, where the per-session/per-turn LLM tax makes them real.\n")
    a("> Reproduce: `python3 run_live_graphify.py && python3 render_report.py`. "
      "Re-measure: `measure_graphify_real.md`. Full provenance: `CONTEXT.md`.\n")
    return "\n".join(L)


def main() -> None:
    safety.assert_non_invasive()
    live, cal = _load()
    for name, text in [("REPORT.md", render_concise(live, cal)),
                       ("REPORT_DETAILED.md", render_detailed(live, cal))]:
        out = BENCH / name
        safety.assert_write_local(out)
        out.write_text(text)
        print("wrote", out.name)


if __name__ == "__main__":
    main()
