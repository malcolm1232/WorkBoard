#!/usr/bin/env python3
"""Dense baseline on the SAME BEIR dataset, measured by us — so the BM25-vs-dense
comparison is apples-to-apples on a public benchmark (not cited from memory).

Embeds the BEIR corpus + queries with OpenAI text-embedding-3-small (1536-d),
cosine-ranks, reports nDCG@10 / Recall@k. Vectors cached (key never stored).

    export OPENAI_API_KEY="$(cat '/path/to/key.txt')"
    python3 beir_dense.py scifact
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request

import numpy as np
from beir_eval import load, ndcg_at_k, recall_at_k  # reuse loaders + metrics

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = "text-embedding-3-small"


def _raw(ds):
    """Return (ids, raw_doc_texts, raw_query_texts) — untokenised, for embedding."""
    base = os.path.join(HERE, "inputs", ds)
    ids, docs = [], []
    for line in open(os.path.join(base, "corpus.jsonl")):
        d = json.loads(line)
        ids.append(d["_id"])
        docs.append(((d.get("title", "") + ". " + d.get("text", "")).strip())[:6000])
    return ids, docs


def embed(texts):
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("OPENAI_API_KEY not set")
    out = []
    for i in range(0, len(texts), 256):
        body = json.dumps({"model": MODEL, "input": texts[i:i + 256]}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body,
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.load(r)
                break
            except Exception as e:
                if attempt == 3:
                    raise
                print(f"  retry {attempt+1}: {e}", file=sys.stderr)
        out.extend(d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"]))
        print(f"  embedded {min(i+256, len(texts))}/{len(texts)}", file=sys.stderr)
    return out


def cached_embed(tag, texts):
    cache_path = os.path.join(HERE, "results", f"dense_cache_{tag}.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    keys = [hashlib.sha1(t.encode()).hexdigest() for t in texts]
    need_idx = [i for i, k in enumerate(keys) if k not in cache]
    if need_idx:
        vecs = embed([texts[i] for i in need_idx])
        for i, v in zip(need_idx, vecs):
            cache[keys[i]] = [round(x, 6) for x in v]
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        json.dump(cache, open(cache_path, "w"))
    return np.array([cache[k] for k in keys])


def main(ds):
    ids, docs = _raw(ds)
    _, _, queries, qrels = load(ds, "test")   # tokenised queries unused; reload raw below
    # raw query text for embedding
    qtext = {}
    for line in open(os.path.join(HERE, "inputs", ds, "queries.jsonl")):
        d = json.loads(line)
        if d["_id"] in qrels:
            qtext[d["_id"]] = d["text"]
    print(f"Embedding {len(docs)} docs + {len(qtext)} queries ({MODEL})…", file=sys.stderr)
    D = cached_embed(f"{ds}_docs", docs); D /= np.linalg.norm(D, axis=1, keepdims=True)
    qids = list(qtext)
    Q = cached_embed(f"{ds}_queries", [qtext[q] for q in qids])
    Q /= np.linalg.norm(Q, axis=1, keepdims=True)
    nd, r10, r100 = [], [], []
    for j, q in enumerate(qids):
        order = np.argsort(-(D @ Q[j]))[:100]
        ranked = [ids[i] for i in order]
        nd.append(ndcg_at_k(ranked, qrels[q], 10))
        r10.append(recall_at_k(ranked, qrels[q], 10))
        r100.append(recall_at_k(ranked, qrels[q], 100))
    n = len(qids)
    res = {"dataset": ds, "model": MODEL, "n_queries": n, "n_docs": len(ids),
           "nDCG@10": round(sum(nd) / n, 4),
           "Recall@10": round(sum(x for x in r10 if x is not None) / n, 4),
           "Recall@100": round(sum(x for x in r100 if x is not None) / n, 4)}
    json.dump(res, open(os.path.join(HERE, "results", f"dense_{ds}.json"), "w"), indent=2)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "scifact")
