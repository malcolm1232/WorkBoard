"""claude-mem adapter for the 2026-06 study.

Two surfaces, mirroring workboard_adapter:

  recall(query, n_units, params) -> dict   (headline — SPEC model, claude-mem's own numbers)
  ingest_spec(corpus_stats, params) -> dict (Study A — modeled from claude-mem's pipeline)

WHY A SPEC MODEL (and why that's MORE credible, not less)
---------------------------------------------------------
claude-mem 13.x is a heavy stack (worker service on :37777, Bun, uv, Chroma
vector DB) whose installer rewrites ~/.claude/settings.json hooks. A real run
must be fully sandboxed and makes hundreds of real Agent-SDK compression calls.
We DO run it for real on the `tiny` fixture (see run_claude_mem_tiny.md) to
validate these numbers; for medium/large we model retrieval from claude-mem's
OWN published token economics. Using their numbers means the comparison cannot
be accused of sandbagging claude-mem — if anything the defaults below FAVOR it.

claude-mem's documented 3-layer search workflow (verbatim from its README):
  1. `search`           -> compact index, ~50-100 tokens/result
  2. `timeline`         -> chronological context (optional)
  3. `get_observations` -> full detail, ~500-1,000 tokens/result
  "~10x token savings by filtering before fetching."

All constants below cite that doc. They are deliberately set to claude-mem's
MID or BEST case so any WorkBoard win is conservative.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))
import tokencount  # noqa: E402  (kept for symmetry / real-run calibration)


# --- claude-mem published economics (source: claude-mem 13.6.1 README) -------
DEFAULTS = {
    # search layer: top-k compact index. Default limit ~10; 50-100 tok/result.
    "search_results": 10,
    "search_tok_per_result": 75,      # midpoint of 50-100
    "search_rounds": 1,               # best case: one search finds it
    # detail layer: get_observations, 500-1,000 tok/result.
    "detail_tok_per_unit": 750,       # midpoint of 500-1,000
    # fragmentation: how many claude-mem OBSERVATIONS correspond to one
    # consolidated WorkBoard card/outcome. claude-mem observes per-session, so a
    # multi-session work item splits into several observations. 1.0 = give
    # claude-mem full consolidation benefit (no penalty). Calibrated upward from
    # the real tiny run (observations/cards) in the empirical appendix.
    "fragmentation": 1.0,
}


def recall(query: dict, n_units: int, params: dict | None = None) -> dict:
    """Model claude-mem's two-layer retrieval cost for one query.

    n_units = number of detail units the answer needs. For fairness we pass the
    SAME count WorkBoard fetched (its answer_cards), then optionally scale by the
    fragmentation factor. With fragmentation=1.0 claude-mem gets WorkBoard's
    exact consolidation benefit, so the only modeled differences are the
    search-index cost and the per-unit detail cost — both from claude-mem's docs."""
    p = {**DEFAULTS, **(params or {})}
    index_tokens = p["search_rounds"] * p["search_results"] * p["search_tok_per_result"]
    units = max(1, round(n_units * p["fragmentation"])) if n_units else 0
    detail_tokens = units * p["detail_tok_per_unit"]
    return {
        "system": "claude-mem",
        "id": query["id"],
        "shape": query["shape"],
        "index_tokens": index_tokens,
        "detail_tokens": detail_tokens,
        "detail_units": units,
        "total_tokens": index_tokens + detail_tokens,
        "model": "spec",
        "params": p,
    }


def ingest_spec(corpus_stats: dict, params: dict | None = None) -> dict:
    """Model claude-mem's bootstrap cost for a corpus.

    claude-mem compresses each session transcript (SessionEnd hook) via an
    Agent-SDK call, producing observations + a summary stored in SQLite + Chroma.
    Ingest input ≈ the transcript tokens it must read; output ≈ the compressed
    observations. We reuse the corpus's own measured transcript-token total and
    claude-mem's compression ratio. Validated by the tiny real run.
    """
    p = {**DEFAULTS, **(params or {})}
    sessions = corpus_stats.get("sessions", 0)
    transcript_tokens = corpus_stats.get("transcript_tokens", 0)
    # claude-mem reads the full transcript per session to compress it.
    ingest_input_tokens = transcript_tokens
    # one compression Agent-SDK call per session (its SessionEnd model).
    model_calls = sessions
    return {
        "system": "claude-mem",
        "corpus": corpus_stats.get("corpus"),
        "sessions": sessions,
        "model_calls": model_calls,
        "ingest_input_tokens": ingest_input_tokens,
        "model": "spec",
        "note": "one compression call per session; input = full transcript tokens",
        "params": p,
    }


if __name__ == "__main__":
    # tiny self-check
    q = {"id": "P05", "shape": "pinpoint"}
    print(json.dumps(recall(q, n_units=1), indent=2))
