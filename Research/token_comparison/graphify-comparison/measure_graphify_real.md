# Measuring graphify for real (sandboxed) — reproducible recipe

Every number in `results/raw/calibration.json` came from this procedure. It runs
graphify **for real** but under a throwaway `$HOME`, so the real `~/.claude` and
the WorkBoard product are never touched. Self-contained: all inputs (code corpus,
frozen SKILL.md, board snapshot) already live in this folder.

## Why sandboxed
`graphify install` writes a SKILL + a `CLAUDE.md` line into `$HOME/.claude`. We
point `$HOME` at a disposable dir so that install is contained and auditable.

## Steps (run from this folder)

```bash
DST="$(pwd)"                                   # .../graphify-comparison
SBX="$DST/sandbox"; GHOME="$SBX/home"; VBIN="$SBX/venv/bin"

# 1. throwaway venv (graphify needs Python 3.10+; measured with 3.13)
python3.13 -m venv "$SBX/venv"
"$VBIN/pip" install --upgrade pip
"$VBIN/pip" install graphifyy                  # PyPI name is `graphifyy` (0.8.41 here)

# 2. sandbox HOME so the install can't touch the real ~/.claude
export HOME="$GHOME"; mkdir -p "$GHOME"

# 3. install the Claude integration INTO the sandbox, then inspect the footprint
"$VBIN/graphify" install --platform claude
find "$GHOME/.claude" -type f          # -> CLAUDE.md line + skills/graphify/{SKILL.md,references/*}
test ! -f "$GHOME/.claude/settings.json" && echo "confirmed: NO settings.json / NO hook"

# 4. build the graph on the frozen code corpus (local AST, no LLM)
( cd "$DST/fixtures/code_corpus" && "$VBIN/graphify" . )   # -> graphify-out/graph.json

# 5. measure per-query subgraph payload (BFS, local, no LLM)
G="$DST/fixtures/code_corpus/graphify-out/graph.json"
"$VBIN/graphify" query "what connects card state to the board lock?" --graph "$G"
# ...repeat for the 5-query set; tokenize each output with tokencount.py
```

The exact tokenization + `calibration.json` write is the inline Python block used
to build this study (see `CONTEXT.md` §"Step-by-step"). After capturing
calibration, regenerate everything offline:

```bash
python3 run_live_graphify.py && python3 render_report.py
```

## What we tokenize (shared `tokencount.py`, tiktoken cl100k — same as WorkBoard)
| Constant in calibration.json | Source |
|---|---|
| `graphify.always_on_per_session_tok` | `$GHOME/.claude/CLAUDE.md` |
| `graphify.skill_md_tok` | `$GHOME/.claude/skills/graphify/SKILL.md` |
| `graphify.references_total_tok` | sum of `skills/graphify/references/*.md` |
| `graphify.query_subgraph_mean_tok` | mean tokens of 5 `graphify query` outputs |
| `graphify.write_api_tokens` | 0 — code rebuild is local tree-sitter AST |
| `graphify.per_prompt_injection_tok` | 0 — no PreToolUse hook installed |

## WorkBoard constants (same snapshot, same tokenizer)
- `skill_md_tok` (5,898) — tokenized from `fixtures/workboard_SKILL.md` (frozen copy
  of `WorkBoard/SKILL.md`).
- `per_prompt_nudge_tok` (306) / trimmed (40) / `always_on` digest (97) — measured
  in the prior studies from `scripts/hook_user_prompt.sh` + `card.py digest`.
- `recall_mean_tok` (2,399) — reused from the claude-mem study on the **same**
  frozen `board_snapshot.json` (work-outcome queries, n=19).

## Teardown
`rm -rf "$SBX"` removes the venv + sandbox HOME (git-ignored anyway). The frozen
evidence — `graph.json`, `calibration.json`, the reports — stays.

## Pin / drift
graphify is fast-moving (PyPI `graphifyy`, name reclaim pending). Numbers here are
**0.8.41**. Re-run this recipe after upgrades — **if a future version adds a real
PreToolUse hook, the per-prompt row changes** and the report must be regenerated.
