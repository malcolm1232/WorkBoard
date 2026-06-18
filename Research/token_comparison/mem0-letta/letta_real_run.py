"""REAL local Letta measurement (runs under .letta-venv, Python 3.13).

The user asked for Letta to be measured by a REAL run rather than modeled from a
spec. Letta is the one peer cheap enough to run truly locally: backend = Ollama
`llama3.2:3b`, embeddings = Ollama `nomic-embed-text`, SQLite — no API key, no cost.

What we measure (and why it's defensible even with a tiny local model):

  (A) PER-TURN IN-CONTEXT OVERHEAD  — the headline, deterministic.
      Letta's core memory blocks (2 × 5000-char) + the memory-tool JSON schemas +
      the base system prompt are re-sent to the model EVERY turn. We retrieve the
      real agent state and tiktoken-count (cl100k — the SAME tokenizer the rest of
      the harness uses) the exact in-context payload. This does NOT depend on the
      model behaving; it's a structural property of how Letta runs.

  (B) MODEL-REPORTED USAGE  — corroboration.
      We replay real session turns through the agent and read Letta's own
      LettaUsageStatistics (prompt/completion/step_count) + count how many memory
      tool calls the agent actually emits. Llama-3B's *judgement* is weak, but this
      confirms the mechanism is real (blocks resent each turn; writes are tool
      calls) and gives a real-world token figure alongside the fair tiktoken one.

Writes results/raw/letta_real.json. Read by peers/letta_adapter.py (py3.9 harness).
Cost-bounded: a few sessions × few turns by default; the cap is logged, not silent.
"""

from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

import tiktoken
from letta_client import Letta

import os
BENCH = Path(__file__).resolve().parent
CORPORA = BENCH / "corpora"
# Writes a SEPARATE corroboration file so the deterministic, reproducible
# letta_real.json (from letta_incontext_real.py) stays the headline.
OUT = BENCH / "results" / "raw" / "letta_modelrun.json"
BASE_URL = os.environ.get("LETTA_BASE_URL", "http://localhost:8283")

ENC = tiktoken.get_encoding("cl100k_base")
def tk(s: str) -> int:
    return len(ENC.encode(s or "", disallowed_special=()))

MEMORY_TOOLS = {
    "core_memory_append", "core_memory_replace", "memory_replace",
    "memory_insert", "memory_rethink", "archival_memory_insert",
    "archival_memory_search", "conversation_search",
}


def _msg_text(o: dict) -> str:
    m = o.get("message")
    if isinstance(m, dict):
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(b.get("text", "") for b in c
                            if isinstance(b, dict) and b.get("type") == "text")
    if isinstance(o.get("content"), str):
        return o["content"]
    return ""


def load_sessions(corpus: str, n_sessions: int, n_turns: int, max_chars: int = 1200):
    tdir = CORPORA / corpus / "transcripts"
    files = sorted(tdir.glob("*.jsonl"))
    sessions = []
    for jf in files:
        turns = []
        for line in jf.open(errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "user":
                t = _msg_text(o).strip()
                if t and not t.startswith("<"):           # skip tool/system noise
                    turns.append(t[:max_chars])
            if len(turns) >= n_turns:
                break
        if len(turns) >= 2:                                # need a couple real turns
            sessions.append({"sid": jf.stem, "turns": turns[:n_turns]})
        if len(sessions) >= n_sessions:
            break
    return sessions


def pick_handles(client: Letta):
    """Find the Ollama LLM + embedding handles the server exposes."""
    llm = embed = None
    for m in client.models.list():
        h = getattr(m, "handle", None) or getattr(m, "model", "")
        if h and "llama3.2" in h and llm is None:
            llm = h
    for e in client.models.embeddings.list():
        h = getattr(e, "handle", None) or getattr(e, "model", "")
        if h and "nomic" in h and embed is None:
            embed = h
    return llm, embed


def measure_incontext(client: Letta, agent_id: str) -> dict:
    """(A) deterministic per-turn in-context overhead, tiktoken cl100k."""
    a = client.agents.retrieve(agent_id)
    system_txt = getattr(a, "system", "") or ""
    blocks = []
    mem = getattr(a, "memory", None)
    block_list = getattr(mem, "blocks", None) or getattr(a, "memory_blocks", []) or []
    for b in block_list:
        label = getattr(b, "label", "")
        value = getattr(b, "value", "") or ""
        limit = getattr(b, "limit", None)
        # Letta frames each block as a labelled section in context; count value +
        # a small label wrapper. We count the VALUE (what actually fills) and the
        # LIMIT capacity separately so the report can show full-capacity worst case.
        blocks.append({"label": label, "value_tokens": tk(value),
                       "limit_chars": limit, "limit_tokens_est": int((limit or 0) / 4)})
    # tools carried in context (their JSON schemas are sent every turn)
    tools = getattr(a, "tools", []) or []
    tool_tokens = 0
    mem_tool_tokens = 0
    tool_names = []
    for t in tools:
        name = getattr(t, "name", "")
        schema = getattr(t, "json_schema", None)
        s_tok = tk(json.dumps(schema)) if schema else tk(name)
        tool_tokens += s_tok
        tool_names.append(name)
        if name in MEMORY_TOOLS:
            mem_tool_tokens += s_tok
    system_tokens = tk(system_txt)
    block_value_tokens = sum(b["value_tokens"] for b in blocks)
    block_limit_tokens = sum(b["limit_tokens_est"] for b in blocks)
    # current = what's actually in context now (blocks lightly filled at start);
    # full_capacity = worst case if both 5000-char blocks fill up.
    per_turn_current = system_tokens + block_value_tokens + tool_tokens
    per_turn_full = system_tokens + block_limit_tokens + tool_tokens
    return {
        "system_tokens": system_tokens,
        "block_value_tokens": block_value_tokens,
        "block_limit_tokens_est": block_limit_tokens,
        "tool_schema_tokens": tool_tokens,
        "memory_tool_schema_tokens": mem_tool_tokens,
        "n_blocks": len(blocks),
        "n_tools": len(tools),
        "tool_names": tool_names,
        "per_turn_incontext_current": per_turn_current,
        "per_turn_incontext_full_capacity": per_turn_full,
        "blocks": blocks,
    }


def replay(client: Letta, agent_id: str, sessions, per_msg_timeout: int):
    """(B) real model-reported usage + real memory-tool-call counts."""
    per_session = []
    for s in sessions:
        ps = {"sid": s["sid"], "turns": 0, "prompt_tokens": 0,
              "completion_tokens": 0, "total_tokens": 0, "steps": 0,
              "memory_tool_calls": 0, "errors": 0}
        for turn in s["turns"]:
            try:
                resp = client.agents.messages.create(
                    agent_id=agent_id,
                    messages=[{"role": "user", "content": turn}],
                    max_steps=6,
                    timeout=per_msg_timeout,
                )
            except Exception as e:
                ps["errors"] += 1
                print(f"    turn error ({s['sid'][:8]}): {type(e).__name__}", flush=True)
                continue
            ps["turns"] += 1
            u = getattr(resp, "usage", None)
            if u:
                ps["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                ps["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
                ps["total_tokens"] += getattr(u, "total_tokens", 0) or 0
                ps["steps"] += getattr(u, "step_count", 0) or 0
            for m in (getattr(resp, "messages", []) or []):
                tc = getattr(m, "tool_call", None)
                name = getattr(tc, "name", None) if tc else None
                if name in MEMORY_TOOLS:
                    ps["memory_tool_calls"] += 1
        per_session.append(ps)
        print(f"  session {s['sid'][:8]}: {ps['turns']} turns, "
              f"{ps['total_tokens']} tok, {ps['steps']} steps, "
              f"{ps['memory_tool_calls']} mem-tool-calls, {ps['errors']} err", flush=True)
    return per_session


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="tiny")
    ap.add_argument("--sessions", type=int, default=4)
    ap.add_argument("--turns", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    client = Letta(base_url=BASE_URL)
    llm, embed = pick_handles(client)
    print(f"handles: llm={llm}  embed={embed}", flush=True)
    if not llm or not embed:
        print("ERROR: could not find ollama llm/embed handles on the server.", flush=True)
        sys.exit(2)

    agent = client.agents.create(
        name=f"wb-vs-letta-live-{int(time.time())}",
        memory_blocks=[
            {"label": "human", "value": "Name: the user. A software engineer "
             "working on the WorkBoard project.", "limit": 5000},
            {"label": "persona", "value": "I am a helpful assistant that helps "
             "track and remember the user's engineering work as it happens.",
             "limit": 5000},
        ],
        model=llm,
        embedding=embed,
        include_base_tools=True,
    )
    aid = agent.id
    print(f"agent: {aid}", flush=True)

    incontext = measure_incontext(client, aid)
    print(f"per-turn in-context (current/full): "
          f"{incontext['per_turn_incontext_current']}/"
          f"{incontext['per_turn_incontext_full_capacity']} tok "
          f"(system {incontext['system_tokens']}, tools {incontext['tool_schema_tokens']}, "
          f"{incontext['n_tools']} tools)", flush=True)

    sessions = load_sessions(args.corpus, args.sessions, args.turns)
    print(f"replaying {len(sessions)} sessions × ≤{args.turns} turns "
          f"(corpus={args.corpus}; CAP logged, not silent)", flush=True)
    per_session = replay(client, aid, sessions, args.timeout)

    # aggregates
    done = [p for p in per_session if p["turns"]]
    n = len(done) or 1
    tot_turns = sum(p["turns"] for p in done) or 1
    write_tool_calls_mean = sum(p["memory_tool_calls"] for p in done) / n
    model_total_per_session = sum(p["total_tokens"] for p in done) / n
    model_total_per_turn = sum(p["total_tokens"] for p in done) / tot_turns
    # estimate write tool-call OUTPUT tokens: completion attributable to mem tools.
    # We can't perfectly split; report mean memory tool calls and a per-call est.
    out = {
        "backend": f"ollama/{llm}  embed:ollama/{embed}",
        "letta_version": __import__("letta").__version__,
        "tokenizer_incontext": "tiktoken/cl100k_base (fair, same as harness)",
        "tokenizer_model_reported": "llama-3.2 tokenizer (model-reported usage)",
        "corpus": args.corpus,
        "sessions_run": len(done),
        "turns_run": tot_turns,
        "cap_note": f"REAL run cost-bounded to {args.sessions} sessions × {args.turns} "
                    "turns of the tiny corpus through a local 3B model; per-turn "
                    "in-context overhead (A) is exact, model-reported usage (B) is a "
                    "real sample projected by the harness.",
        "incontext": incontext,
        "per_session_detail": per_session,
        "live": {
            # FAIR headline: deterministic per-turn in-context overhead (tiktoken)
            "per_turn_incontext_tiktoken": incontext["per_turn_incontext_current"],
            "per_turn_incontext_full_capacity": incontext["per_turn_incontext_full_capacity"],
            "per_turn_incontext_breakdown": {
                "system": incontext["system_tokens"],
                "blocks": incontext["block_value_tokens"],
                "blocks_full_capacity": incontext["block_limit_tokens_est"],
                "tool_schemas": incontext["tool_schema_tokens"],
                "memory_tool_schemas": incontext["memory_tool_schema_tokens"],
            },
            "writes_per_session_mean": round(write_tool_calls_mean, 2),
            "write_tool_tokens_per_session_mean": None,   # see model-reported completion
            # corroboration (model-reported, llama tokenizer)
            "model_reported_total_per_session_mean": round(model_total_per_session, 1),
            "model_reported_total_per_turn_mean": round(model_total_per_turn, 1),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {OUT}", flush=True)
    # cleanup the throwaway agent
    try:
        client.agents.delete(aid)
    except Exception:
        pass


if __name__ == "__main__":
    main()
