#!/usr/bin/env python3
"""H4 — REAL dense-vector baseline (OpenAI text-embedding-3-small, 1536-d).

The honest head-to-head: embed our 533 cards + the gold queries with the SAME
embedding model mem0 and Letta default to, rank by cosine, and measure recall@k
by shape on OUR corpus. This replaces the "modeled" peer column in REPORT.md with
a MEASURED dense baseline — so the "vectors win thematic, lose pinpoint" claim is
empirical, not asserted.

Key handling: reads OPENAI_API_KEY from the env (set by the caller from the
throwaway key file). The key is NEVER written to disk or printed. Embeddings are
cached to results/dense_cache.json (vectors only) so re-runs are free + offline +
reproducible. Delete that file to force a fresh embed.

Run:
    export OPENAI_API_KEY="$(cat '/Users/malco/Desktop/temp throwaway key.txt')"
    python3 dense_eval.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "inputs", "board_snapshot.json")
QUERIES = os.path.join(HERE, "inputs", "queries.json")
CACHE = os.path.join(HERE, "results", "dense_cache.json")
MODEL = "text-embedding-3-small"
KS = [1, 3, 5]


def card_text(c: dict) -> str:
    """What a vector memory would store for a card — title + origin + notes +
    writeup + subtasks (the same content the lexical matcher indexes), truncated
    to a safe embedding length."""
    parts = [
        f"#{c['num']}", c.get("title", "") or "", c.get("code", "") or "",
        " ".join(c.get("tags") or []), c.get("origin") or "", c.get("notes") or "",
        c.get("writeup") or "",
        " ".join(s.get("text", "") for s in (c.get("subtasks") or [])),
    ]
    return " ".join(p for p in parts if p)[:6000]


def embed(texts: list[str]) -> list[list[float]]:
    """Batch-embed via the OpenAI REST API (urllib, no SDK)."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY not set — export it from the key file first.")
    out = []
    for i in range(0, len(texts), 256):
        batch = texts[i:i + 256]
        body = json.dumps({"model": MODEL, "input": batch}).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/embeddings", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        out.extend(d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"]))
        print(f"  embedded {min(i + 256, len(texts))}/{len(texts)}", file=sys.stderr)
    return out


def get_embeddings(items: dict[str, str]) -> dict[str, list[float]]:
    """items: key -> text. Cache by sha1(text) so identical text is never re-billed."""
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    need = {k: t for k, t in items.items() if hashlib.sha1(t.encode()).hexdigest() not in cache}
    if need:
        keys = list(need)
        vecs = embed([need[k] for k in keys])
        for k, v in zip(keys, vecs):
            cache[hashlib.sha1(need[k].encode()).hexdigest()] = [round(x, 6) for x in v]
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump(cache, open(CACHE, "w"))
    return {k: cache[hashlib.sha1(t.encode()).hexdigest()] for k, t in items.items()}


def main():
    cards = json.load(open(SNAP))["cards"]
    queries = json.load(open(QUERIES))["queries"]
    scored = [q for q in queries if any(re.fullmatch(r"#\d+", g) for g in q["gold_ids"])]

    cmap = {str(c["num"]): card_text(c) for c in cards}
    qmap = {q["id"]: q["q"] for q in scored}
    print("Embedding %d cards + %d queries (%s)…" % (len(cmap), len(qmap), MODEL), file=sys.stderr)
    cemb = get_embeddings(cmap)
    qemb = get_embeddings(qmap)

    nums = [c["num"] for c in cards]
    C = np.array([cemb[str(n)] for n in nums]); C /= np.linalg.norm(C, axis=1, keepdims=True)

    hit = {k: 0 for k in KS}; shapes = {}
    for q in scored:
        gold = {int(g[1:]) for g in q["gold_ids"] if re.fullmatch(r"#\d+", g)}
        qv = np.array(qemb[q["id"]]); qv /= np.linalg.norm(qv)
        order = np.argsort(-(C @ qv))
        ranked = [nums[i] for i in order]
        s = shapes.setdefault(q["shape"], {"n": 0, "h3": 0, "h5": 0})
        s["n"] += 1
        for k in KS:
            if gold & set(ranked[:k]):
                hit[k] += 1
        if gold & set(ranked[:3]): s["h3"] += 1
        if gold & set(ranked[:5]): s["h5"] += 1
    n = len(scored)
    res = {"model": MODEL, "n": n,
           "overall": {f"hit@{k}": round(hit[k] / n, 3) for k in KS},
           "by_shape": {sh: {"n": s["n"], "hit@3": round(s["h3"] / s["n"], 3),
                             "hit@5": round(s["h5"] / s["n"], 3)} for sh, s in shapes.items()}}
    json.dump(res, open(os.path.join(HERE, "results", "dense_results.json"), "w"), indent=2)
    print("\nH4 dense (OpenAI %s) over %d gold queries:" % (MODEL, n))
    print("  overall  " + "  ".join(f"hit@{k}={hit[k]/n:.3f}" for k in KS))
    for sh, s in res["by_shape"].items():
        print(f"  {sh:<9} n={s['n']}  hit@3={s['hit@3']:.3f}  hit@5={s['hit@5']:.3f}")


if __name__ == "__main__":
    main()
