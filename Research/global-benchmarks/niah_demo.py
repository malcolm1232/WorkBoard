#!/usr/bin/env python3
"""Long-context angle (NIAH) — the honest framing of 'vs Claude / ChatGPT'.

Needle-in-a-Haystack asks: plant a fact in a long context, can you find it?
Frontier LLMs (Claude, GPT, Gemini) score ~99-100% single-needle — by holding the
WHOLE haystack in the context window and attending to it (you pay for every token,
every query). A RETRIEVER instead finds the needle and feeds only that to the
model (a few tokens). This demo shows our BM25 core does single-needle NIAH at
~100% across depths/lengths for ~0 cost.

IMPORTANT (stated plainly): this is NOT a claim that WorkBoard 'beats' Claude at
long context. NIAH-single-needle is a *retrieval* task and is easy for lexical
search. RULER's hard tasks — multi-hop, variable tracking, aggregation, 'how many
times did X' — test the MODEL's in-context REASONING, which a retriever cannot do.
A retriever and an LLM are different layers: WorkBoard decides what enters the
window; Claude reasons over it. They compose; they don't compete.

    python3 niah_demo.py
"""
from __future__ import annotations

import json
import os

from beir_eval import BM25, toks

HERE = os.path.dirname(os.path.abspath(__file__))
NEEDLE_ID = "NEEDLE"
NEEDLE = "The secret passcode for the vault is BANANA-47-DELTA."
QUERY = "What is the secret passcode for the vault?"


def filler():
    """Distractor sentences mined from the LOCOMO turns (realistic prose)."""
    data = json.load(open(os.path.join(HERE, "inputs", "locomo10.json")))
    out = []
    for conv in data:
        for key in conv["conversation"]:
            if key.startswith("session_") and not key.endswith("date_time"):
                for t in conv["conversation"][key]:
                    out.append(t.get("text", ""))
    return [s for s in out if s][:4000]


def run():
    fill = filler()
    rows = []
    for size in (100, 1000, 4000):              # haystack sizes (chunks ≈ tokens×~15)
        for depth in (0.0, 0.25, 0.5, 0.75, 1.0):
            chunks = list(fill[:size])
            pos = min(int(depth * size), size - 1)
            ids = [f"f{i}" for i in range(size)]
            chunks.insert(pos, NEEDLE); ids.insert(pos, NEEDLE_ID)
            bm = BM25([toks(c) for c in chunks])
            top = [ids[i] for i in bm.search(toks(QUERY), k=1)]
            rows.append((size, depth, top and top[0] == NEEDLE_ID))
    acc = sum(1 for _, _, ok in rows if ok) / len(rows)
    print(f"NIAH single-needle retrieval (BM25 core): {sum(ok for *_, ok in rows)}/{len(rows)} "
          f"= {acc:.0%} top-1 across {len(rows)} (size × depth) cells")
    for size in (100, 1000, 4000):
        cells = [ok for s, _, ok in rows if s == size]
        print(f"  haystack {size:>4} chunks: {sum(cells)}/{len(cells)} found at top-1")
    json.dump({"top1_accuracy": acc, "cells": len(rows)},
              open(os.path.join(HERE, "results", "niah.json"), "w"), indent=2)
    print("\nContext: frontier LLMs (Claude/GPT/Gemini) ~99-100% on this by holding the whole "
          "haystack in-context (pay per token); the retriever finds it for ~0 tokens. "
          "RULER's reasoning tasks are out of a retriever's scope — they test the model.")


if __name__ == "__main__":
    run()
