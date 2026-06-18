"""mem0 adapter for the graphify-comparison study.

Three surfaces, mirroring claude_mem_adapter:

  live(session_stats, params)   -> dict   (PRIMARY — steady-state per-session cost)
  recall(query, n_units, params)-> dict   (cross-check — mem0's flat retrieval bundle)
  ingest_spec(corpus_stats, p)  -> dict   (secondary — bootstrap, de-emphasized)

WHY A SPEC MODEL (and why it's MORE credible, not less)
-------------------------------------------------------
mem0 needs an OpenAI API key + a vector store (Qdrant) + an embedding model to run
for real. Rather than stand that up (and risk being accused of mis-configuring a
competitor), we model mem0 from ITS OWN PUBLISHED numbers — the exact figures mem0
markets. If anything the defaults below FAVOR mem0, so any WorkBoard win is a
conservative floor. This is the same discipline used for the claude-mem peer.

mem0's published economics (sources cited inline):
  - arXiv:2504.19413 "Mem0: Building Production-Ready AI Agents …" (Apr 2025)
  - https://mem0.ai/research-3  (the headline blog)
  LOCOMO head-to-head vs a full-context baseline:
    * ~1.8K tokens injected per conversation  (vs ~26K full-context  → their "90%")
    * p95 latency 1.44s  (vs 17.12s full-context → their "91%")
    * +26% LLM-as-judge accuracy over OpenAI's memory
  Write path: "single-pass ADD-only extraction" = ONE LLM extraction call per add,
  reading the conversation messages, plus one embedding pass.
  Long-context variant (BEAM 1M/10M) cites ~6.7–6.9K tok/query — noted but NOT used
  for the headline (we take the smaller 1.8K, mem0's best case).

KEY ASYMMETRY vs WorkBoard
--------------------------
mem0's retrieval is genuinely cheap and selective (1.8K flat). Its cost is on the
WRITE side: every `add()` spends an LLM extraction call. WorkBoard inverts this —
carding is inline in the agent's normal turn (0 dedicated LLM calls), and recall is
a structured lookup. So the live head-to-head turns on the write tax.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))
import tokencount  # noqa: E402  (kept for symmetry / real-run calibration)


# --- mem0 published economics (sources above) --------------------------------
DEFAULTS = {
    # Retrieval: mem0 injects a flat top-k memory bundle per query. We use mem0's
    # OWN headline LOCOMO number (best case for mem0). Independent of how many
    # facts the answer needs — generous to mem0 on multi-fact/lifecycle queries,
    # where a flat 1.8K is cheaper than scaling per unit.
    "recall_tokens_per_query": 1800,      # mem0.ai/research-3 ("~1.8K vs 26K")
    "recall_beam_long_context": 6719,     # BEAM 1M variant — noted, not headline
    # Write: single-pass ADD extraction = one LLM call per add, reading the
    # conversation messages; plus one embedding pass (counted as a call).
    "add_llm_calls_per_session": 1,       # arXiv:2504.19413 §single-pass ADD
    "embed_calls_per_session": 1,
    # full-context baseline mem0 measures itself against (for the parallel claim)
    "full_context_tokens_per_query": 26000,   # mem0.ai/research-3
}


def recall(query: dict, n_units: int, params: dict | None = None) -> dict:
    """Model mem0's retrieval cost for one query: a flat injected memory bundle.

    n_units is accepted for signature-parity with the other peers but mem0's
    retrieval does NOT scale with it (selective top-k → flat bundle). Giving mem0
    a flat 1.8K regardless of answer fan-out is its best case."""
    p = {**DEFAULTS, **(params or {})}
    total = p["recall_tokens_per_query"]
    return {
        "system": "mem0",
        "id": query["id"],
        "shape": query["shape"],
        "index_tokens": 0,                 # vector search is server-side (no LLM)
        "detail_tokens": total,            # the injected memory bundle
        "detail_units": n_units,
        "total_tokens": total,
        "model": "spec",
        "params": p,
    }


def live(session_stats: dict, params: dict | None = None) -> dict:
    """Per-session steady-state cost of running mem0 as you work.

    session_stats needs: avg_session_input_tokens (the messages each add() reads).
    WRITE  = 1 LLM extraction call/session over the session messages (+1 embed).
    RECALL = recall_tokens_per_query per recall event (handled by the driver).
    mem0 injects NOTHING per-turn (no protocol nudge) — an honest mem0 advantage.
    """
    p = {**DEFAULTS, **(params or {})}
    avg_in = session_stats.get("avg_session_input_tokens", 0)
    return {
        "system": "mem0",
        "write_model_calls": p["add_llm_calls_per_session"],
        "write_embed_calls": p["embed_calls_per_session"],
        "write_model_input_tokens": avg_in,   # add() reads the session messages
        "per_turn_injection": 0,              # no per-turn reminder
        "recall_tokens_per_event": p["recall_tokens_per_query"],
        "model": "spec",
        "params": p,
    }


def ingest_spec(corpus_stats: dict, params: dict | None = None) -> dict:
    """Bootstrap cost (secondary). mem0 ADDs each session via one LLM extraction
    call reading that session's messages — same shape as claude-mem's per-session
    compression, so the bootstrap asymmetry vs WorkBoard is the same story."""
    p = {**DEFAULTS, **(params or {})}
    sessions = corpus_stats.get("sessions", 0)
    transcript_tokens = corpus_stats.get("transcript_tokens", 0)
    return {
        "system": "mem0",
        "corpus": corpus_stats.get("corpus"),
        "sessions": sessions,
        "model_calls": sessions * p["add_llm_calls_per_session"],
        "ingest_input_tokens": transcript_tokens,
        "model": "spec",
        "note": "one single-pass ADD extraction call per session; input = session messages",
        "params": p,
    }


if __name__ == "__main__":
    q = {"id": "P05", "shape": "pinpoint"}
    print(json.dumps(recall(q, n_units=1), indent=2))
    print(json.dumps(live({"avg_session_input_tokens": 5000}), indent=2))
