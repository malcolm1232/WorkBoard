#!/usr/bin/env python3
"""Agent/conversational-memory benchmark — LOCOMO retrieval recall@k.

LOCOMO is the long-conversation memory benchmark mem0 reports on. mem0's headline
(J=66.9%) is END-TO-END answer quality (LLM judge). Here we isolate the RETRIEVAL
step that feeds it: given a question, rank the conversation's dialog turns and
check whether the gold `evidence` turns land in the top-k. That's the apples-to-
apples "does the retriever surface the right memory" measure — for our BM25 core
AND a dense baseline — on the exact corpus mem0 uses.

    python3 locomo_eval.py                 # BM25 only
    export OPENAI_API_KEY=...; python3 locomo_eval.py --dense

Category 5 (adversarial / unanswerable) is excluded from recall (no gold memory).
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
from beir_eval import BM25, toks
from beir_dense import cached_embed

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "inputs", "locomo10.json")
KS = [3, 5, 10]
CAT = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def turns_of(conv):
    """All dialog turns across sessions → [(dia_id, 'Speaker: text')]."""
    out = []
    for key in sorted(k for k in conv if k.startswith("session_") and not k.endswith("date_time")):
        for t in conv[key]:
            out.append((t["dia_id"], f"{t.get('speaker','')}: {t.get('text','')}"))
    return out


def recall_frac(ranked_ids, gold, k):
    return len(set(gold) & set(ranked_ids[:k])) / len(gold) if gold else None


def run(use_dense=False):
    data = json.load(open(DATA))
    agg = {m: {k: [] for k in KS} for m in (["bm25"] + (["dense"] if use_dense else []))}
    bycat = {m: {} for m in agg}
    for ci, conv in enumerate(data):
        turns = turns_of(conv["conversation"])
        ids = [d for d, _ in turns]
        texts = [t for _, t in turns]
        bm = BM25([toks(t) for t in texts])
        if use_dense:
            D = cached_embed(f"locomo_c{ci}_docs", texts); D /= np.linalg.norm(D, axis=1, keepdims=True)
        qs = [qa for qa in conv["qa"] if qa.get("category") != 5 and qa.get("evidence")]
        qtext = [qa["question"] for qa in qs]
        if use_dense and qs:
            Q = cached_embed(f"locomo_c{ci}_q", qtext); Q /= np.linalg.norm(Q, axis=1, keepdims=True)
        for qi, qa in enumerate(qs):
            gold = [e for e in qa["evidence"] if isinstance(e, str) and e in ids]
            if not gold:
                continue
            cat = CAT.get(qa["category"], "?")
            ranked_bm = [ids[i] for i in bm.search(toks(qa["question"]), k=max(KS))]
            for k in KS:
                r = recall_frac(ranked_bm, gold, k)
                agg["bm25"][k].append(r)
                bycat["bm25"].setdefault(cat, {kk: [] for kk in KS})[k].append(r)
            if use_dense:
                order = np.argsort(-(D @ Q[qi]))[:max(KS)]
                ranked_d = [ids[i] for i in order]
                for k in KS:
                    r = recall_frac(ranked_d, gold, k)
                    agg["dense"][k].append(r)
                    bycat["dense"].setdefault(cat, {kk: [] for kk in KS})[k].append(r)

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 4) if xs else None

    res = {"n_questions": len(agg["bm25"][KS[0]]),
           "overall": {m: {f"recall@{k}": mean(agg[m][k]) for k in KS} for m in agg},
           "by_category": {m: {c: {f"recall@{k}": mean(v[k]) for k in KS}
                               for c, v in bycat[m].items()} for m in agg}}
    json.dump(res, open(os.path.join(HERE, "results", "locomo.json"), "w"), indent=2)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    run(use_dense="--dense" in sys.argv)
