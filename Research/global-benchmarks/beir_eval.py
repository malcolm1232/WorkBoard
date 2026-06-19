#!/usr/bin/env python3
"""Put WorkBoard's recall matcher on the GLOBAL scale — run its BM25 core on a
standard BEIR dataset and report the same metric (nDCG@10, Recall@k) that MTEB /
the BEIR leaderboard rank embedding models on. BM25 is itself a canonical BEIR
baseline, so this number is directly comparable to published retrievers.

We evaluate the SAME ranking algorithm that ships in scripts/text_search.py
(Okapi BM25 + optional field/title weighting); the card-specific #ref/literal
boosts simply don't fire on BEIR queries, so this is an honest read of the core.

    python3 beir_eval.py scifact
    python3 beir_eval.py nfcorpus

Deterministic, stdlib+numpy only, reads the downloaded BEIR files under inputs/.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("the a an of to in on at and or for is are was were be been with by as "
            "that this these those it its from we do does did how why what which when "
            "where who whom".split())
K1, B = 0.9, 0.4          # Anserini/BEIR default-ish BM25 params
TITLE_W = 2.0             # field weight: title is the curated summary (BM25F)


def toks(s):
    return [t for t in _WORD.findall((s or "").lower()) if t not in _STOP and len(t) > 1]


def load(ds, split="test"):
    base = os.path.join(HERE, "inputs", ds)
    corpus, ids = [], []
    for line in open(os.path.join(base, "corpus.jsonl")):
        d = json.loads(line)
        ids.append(d["_id"])
        # BM25F: title counted TITLE_W times, then body — same idea as the card
        # matcher weighting the curated title above the long body.
        corpus.append(toks(d.get("title", "")) * int(TITLE_W) + toks(d.get("text", "")))
    queries = {}
    for line in open(os.path.join(base, "queries.jsonl")):
        d = json.loads(line)
        queries[d["_id"]] = toks(d["text"])
    qrels = defaultdict(dict)
    with open(os.path.join(base, "qrels", f"{split}.tsv")) as f:
        next(f)  # header
        for line in f:
            q, c, s = line.split()
            qrels[q][c] = int(s)
    queries = {q: t for q, t in queries.items() if q in qrels}  # eval split only
    return ids, corpus, queries, qrels


class BM25:
    def __init__(self, corpus):
        self.N = len(corpus)
        self.dl = [len(d) for d in corpus]
        self.avgdl = sum(self.dl) / max(self.N, 1)
        self.tf = [Counter(d) for d in corpus]
        df = Counter()
        for d in self.tf:
            for t in d:
                df[t] += 1
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
        self.post = defaultdict(list)            # term -> [(doc, tf)]
        for i, c in enumerate(self.tf):
            for t, f in c.items():
                self.post[t].append((i, f))

    def search(self, qtoks, k=100):
        scores = defaultdict(float)
        for t in set(qtoks):
            if t not in self.idf:
                continue
            idf = self.idf[t]
            for i, f in self.post[t]:
                den = f + K1 * (1 - B + B * self.dl[i] / self.avgdl)
                scores[i] += idf * f * (K1 + 1) / den
        return sorted(scores, key=lambda i: -scores[i])[:k]


def ndcg_at_k(ranked_ids, rel, k=10):
    dcg = sum(rel.get(c, 0) / math.log2(r + 2) for r, c in enumerate(ranked_ids[:k]))
    ideal = sorted(rel.values(), reverse=True)
    idcg = sum(g / math.log2(r + 2) for r, g in enumerate(ideal[:k]))
    return dcg / idcg if idcg else 0.0


def recall_at_k(ranked_ids, rel, k):
    gold = {c for c, s in rel.items() if s > 0}
    if not gold:
        return None
    return len(gold & set(ranked_ids[:k])) / len(gold)


def main(ds):
    split = "test"
    ids, corpus, queries, qrels = load(ds, split)
    bm = BM25(corpus)
    nd, r10, r100 = [], [], []
    for q, qt in queries.items():
        ranked = [ids[i] for i in bm.search(qt, k=100)]
        nd.append(ndcg_at_k(ranked, qrels[q], 10))
        r10.append(recall_at_k(ranked, qrels[q], 10))
        r100.append(recall_at_k(ranked, qrels[q], 100))
    n = len(queries)
    res = {"dataset": ds, "split": split, "n_queries": n, "n_docs": len(ids),
           "nDCG@10": round(sum(nd) / n, 4),
           "Recall@10": round(sum(x for x in r10 if x is not None) / n, 4),
           "Recall@100": round(sum(x for x in r100 if x is not None) / n, 4),
           "params": {"k1": K1, "b": B, "title_weight": TITLE_W}}
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    json.dump(res, open(os.path.join(HERE, "results", f"beir_{ds}.json"), "w"), indent=2)
    print(json.dumps(res, indent=2))
    return res


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "scifact")
