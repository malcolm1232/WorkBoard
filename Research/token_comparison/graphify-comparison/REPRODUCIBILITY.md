# REPRODUCIBILITY

## Determinism
- One tokenizer for everything: `tokencount.py` (tiktoken `cl100k_base`, offline).
  The same counts apply to both systems — no per-system tokenizer advantage.
- `run_live_graphify.py` and `render_report.py` are pure functions of
  `results/raw/calibration.json` — same input → byte-identical `live.json` and
  reports. No network, no clock, no randomness.
- graphify's graph build is local tree-sitter AST (no LLM), so the graph and the
  query subgraphs are deterministic for a fixed corpus + version.

## Two levels of reproduction
1. **Offline (seconds)** — needs only this folder's cached `calibration.json`:
   ```bash
   python3 run_live_graphify.py && python3 render_report.py
   ```
2. **From scratch** — rebuild the venv, graph, and calibration, then step 1:
   follow `measure_graphify_real.md`.

## What is git-ignored (regenerable or private)
The parent `Research/token_comparison/.gitignore` ignores:
- `*/board_snapshot.json` — frozen real board, may contain private data.
- `*/results/raw/` — regenerable measurement output.
- `*/__pycache__/`, `*.pyc`, `*.board.lock`.

This folder's `.gitignore` additionally ignores `sandbox/` (the venv + throwaway
HOME, ~hundreds of MB, machine-specific).

> Because `results/raw/` and `board_snapshot.json` are git-ignored, a **fresh
> clone** must re-measure (level 2) to repopulate them. To preserve the evidence
> across a closed session anyway, **every measured number is also inlined in
> `CONTEXT.md` and `REPORT_DETAILED.md`** — so the result is never lost even
> without the raw files.

## Version pin
Measured with **`graphifyy` 0.8.41**, Python 3.13, on macOS arm64. If graphify
changes its Claude integration (e.g. adds a real PreToolUse hook), the per-prompt
row changes — re-measure and regenerate.

## Non-invasiveness
- graphify ran under a sandbox `$HOME` (`sandbox/home`); the real `~/.claude` was
  never written (verified: no `graphify` skill / no graphify line in real config).
- `lib/safety.py` confines all harness writes to this folder and refuses to write
  the live board (`board/board.json`).
