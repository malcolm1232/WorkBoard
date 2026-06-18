# Real claude-mem ingest — sandboxed validation run (tiny fixture)

This procedure runs claude-mem **for real** on the `tiny` corpus to cross-check
the published per-layer numbers the study uses (Study A ingest + Study B recall
spec). It is **fully sandboxed** — it never touches the real `~/.claude`, the
live board, or the user's claude-mem (if any).

> Why this is optional: the report already uses claude-mem's *own* published
> numbers, which is the more defensible position. This run is a cross-check, not
> the source of the headline.

## Hard requirements (claude-mem 13.x is heavy)

- **Node ≥ 20** (the Mac default here is 18.8.0 — install node 20/22 first, e.g.
  a standalone tarball under `/tmp`, do NOT change the system node).
- **Bun**, **uv** (claude-mem auto-installs these; allow it inside the sandbox).
- **Chroma** vector DB (claude-mem manages it).
- Claude auth / `ANTHROPIC_API_KEY` for its compression Agent-SDK calls.

## Procedure

```bash
set -euo pipefail
STUDY="$(cd "$(dirname "$0")" && pwd)"          # 
SANDBOX="$STUDY/peers/_cm_home"                  # throwaway HOME (git-ignored)
rm -rf "$SANDBOX"; mkdir -p "$SANDBOX/.claude/projects/-Users-malco"

# 1. Feed claude-mem the SAME frozen corpus (symlink the tiny transcripts).
ln -s "$STUDY"/corpora/tiny/transcripts/*.jsonl \
      "$SANDBOX/.claude/projects/-Users-malco/"

# 2. Install + run claude-mem against the sandbox HOME ONLY.
#    HOME override keeps its installer (which rewrites ~/.claude/settings.json
#    and starts the :37777 worker) entirely inside the sandbox.
export HOME="$SANDBOX"
export CLAUDE_MEM_HOME="$SANDBOX/.claude-mem"
cd "$STUDY/peers/_cm_pkg" 2>/dev/null || (mkdir -p "$STUDY/peers/_cm_pkg" && cd "$STUDY/peers/_cm_pkg" && npm init -y >/dev/null && npm install claude-mem)

# 3. Bootstrap-ingest the corpus (compress past transcripts into memory).
#    (Use claude-mem's load/compress entrypoint; see `claude-mem --help`.)
node node_modules/claude-mem/dist/npx-cli/index.js load --all   # or its documented bulk-compress cmd

# 4. Capture the artifact stats to compare with the spec model:
CM_DB="$SANDBOX/.claude-mem"
echo "sqlite bytes : $(du -sk "$CM_DB"/*.db 2>/dev/null | awk '{s+=$1} END{print s*1024}')"
echo "chroma bytes : $(du -sk "$CM_DB"/chroma 2>/dev/null | awk '{s+=$1} END{print s*1024}')"
# observations + summaries counts come from the SQLite tables; query them and
# record: (sessions_compressed, observations, summaries, total compression
# input tokens). Drop these into results/raw/claude_mem_tiny_real.json.
```

## What to record (then re-render)

Write `results/raw/claude_mem_tiny_real.json`:

```json
{
  "corpus": "tiny",
  "sessions_compressed": 0,
  "observations": 0,
  "summaries": 0,
  "ingest_input_tokens_real": 0,
  "avg_observation_tokens_real": 0,
  "avg_search_index_tokens_real": 0,
  "sqlite_bytes": 0,
  "chroma_bytes": 0
}
```

Then set `claude_mem_adapter.DEFAULTS` from the real `avg_*` numbers (or pass them
as `cm_params`) and re-run `render_report.py`. If the real numbers are within the
50-100 / 500-1000 published bands, the spec stands as-is and the headline is
validated. If observations fragment (observations ≫ WorkBoard cards), bump
`fragmentation` accordingly — that only *widens* the WorkBoard margin.

## Cleanup

```bash
rm -rf "$SANDBOX" "$STUDY/peers/_cm_pkg"
```

All sandbox paths are git-ignored; nothing here can leak into the product or the
user's real Claude Code config.
