#!/usr/bin/env python3
"""WorkBoard retrieval bake-off — recall@k + tokens-per-correct, by query shape.

Reproducible, deterministic, READ-ONLY on a FROZEN snapshot (never the live
board). Scores three zero-dep matchers (lexical / bm25 / tfidf_cosine) over the
20 gold recall queries, and measures WorkBoard's structural edge: expanding the
top-3 entry cards along `linkedCards` (the agent "walks the graph").

Run:
    python3 harness.py            # prints report, writes results/*.json + REPORT.md

Outputs land in results/. Inputs are inputs/board_snapshot.json (md5 frozen) +
inputs/queries.json + tokencount.py (tiktoken cl100k, the study's shared tokenizer).
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "matchers"))

import tokencount  # noqa: E402
from matchers import Corpus, rank, MATCHERS, card_doc  # noqa: E402

SNAP = os.path.join(HERE, "inputs", "board_snapshot.json")
QUERIES = os.path.join(HERE, "inputs", "queries.json")
RESULTS = os.path.join(HERE, "results")
KS = [1, 3, 5]


def load():
    cards = json.load(open(SNAP))["cards"]
    queries = json.load(open(QUERIES))["queries"]
    id2num = {c["id"]: c["num"] for c in cards}
    links = {c["num"]: [id2num[i] for i in (c.get("linkedCards") or []) if i in id2num]
             for c in cards}
    return cards, queries, links


def gold_nums(q) -> list[int]:
    return [int(g[1:]) for g in q["gold_ids"] if re.fullmatch(r"#\d+", g)]


def expand(seed: list[int], links: dict, hops: int = 1) -> set[int]:
    """Graph-walk: from seed cards, follow linkedCards `hops` deep."""
    seen = set(seed)
    frontier = set(seed)
    for _ in range(hops):
        nxt = set()
        for n in frontier:
            for m in links.get(n, []):
                if m not in seen:
                    nxt.add(m)
        seen |= nxt
        frontier = nxt
    return seen


def inject_tokens(cards_by_num, top3: list[int]) -> int:
    """Tokens of the <board-steward-recall> index-layer block for top-3 — the
    ONLY thing WorkBoard injects (titles + #ref; detail pulled on demand)."""
    lines = [f"  #{n} {cards_by_num[n]['title']}" for n in top3]
    block = ("<board-steward-recall>Possibly relevant past work — `card.py show <#>` for detail:\n"
             + "\n".join(lines) + "\n</board-steward-recall>")
    return tokencount.count(block)


def evaluate():
    cards, queries, links = load()
    by_num = {c["num"]: c for c in cards}
    corpus = Corpus(cards)
    scored_q = [q for q in queries if gold_nums(q)]  # card-recall scope

    report = {"tokenizer": tokencount.backend_name(), "matchers": {}, "n_scored": len(scored_q),
              "n_total": len(queries)}
    per_query_rows = []

    for m in MATCHERS:
        agg = {"hit": {k: 0 for k in KS},
               "by_shape": {}, "tok_top3": 0, "cov5": 0.0, "cov_traverse": 0.0}
        shapes = {}
        for q in scored_q:
            gold = set(gold_nums(q))
            ranked = rank(m, corpus, q["q"])
            top = {k: ranked[:k] for k in KS}
            hit = {k: bool(gold & set(top[k])) for k in KS}
            cov5 = len(gold & set(top[5])) / len(gold)
            traversed = expand(top[3], links, hops=1)
            cov_tr = len(gold & traversed) / len(gold)
            tok = inject_tokens(by_num, top[3])
            agg["tok_top3"] += tok
            agg["cov5"] += cov5
            agg["cov_traverse"] += cov_tr
            sh = q["shape"]
            s = shapes.setdefault(sh, {"n": 0, "hit": {k: 0 for k in KS}, "cov5": 0.0, "cov_tr": 0.0})
            s["n"] += 1
            for k in KS:
                if hit[k]:
                    agg["hit"][k] += 1
                    s["hit"][k] += 1
            s["cov5"] += cov5
            s["cov_tr"] += cov_tr
            if m == "bm25":
                per_query_rows.append({
                    "id": q["id"], "shape": q["shape"], "q": q["q"][:70],
                    "gold": sorted(gold), "top3": top[3],
                    "hit@3": hit[3], "cov@5": round(cov5, 2),
                    "cov@3+traverse": round(cov_tr, 2), "tok": tok,
                })
        n = len(scored_q)
        report["matchers"][m] = {
            "hit@1": round(agg["hit"][1] / n, 3),
            "hit@3": round(agg["hit"][3] / n, 3),
            "hit@5": round(agg["hit"][5] / n, 3),
            "gold_coverage@5": round(agg["cov5"] / n, 3),
            "gold_coverage@3+traverse": round(agg["cov_traverse"] / n, 3),
            "mean_tokens_top3": round(agg["tok_top3"] / n, 1),
            "tokens_per_correct@3": round(agg["tok_top3"] / max(agg["hit"][3], 1), 1),
            "by_shape": {sh: {
                "n": s["n"],
                "hit@3": round(s["hit"][3] / s["n"], 3),
                "hit@5": round(s["hit"][5] / s["n"], 3),
                "gold_coverage@5": round(s["cov5"] / s["n"], 3),
                "gold_coverage@3+traverse": round(s["cov_tr"] / s["n"], 3),
            } for sh, s in shapes.items()},
        }
    report["per_query_bm25"] = per_query_rows
    os.makedirs(RESULTS, exist_ok=True)
    json.dump(report, open(os.path.join(RESULTS, "bakeoff_results.json"), "w"), indent=2)
    return report


def fmt(report):
    L = []
    L.append(f"Tokenizer: {report['tokenizer']}  ·  scored {report['n_scored']}/{report['n_total']} "
             f"queries (those with ≥1 gold card#)\n")
    L.append(f"{'matcher':<14} {'hit@1':>6} {'hit@3':>6} {'hit@5':>6} {'cov@5':>6} "
             f"{'cov@3+walk':>11} {'tok/q':>7} {'tok/correct':>11}")
    for m, r in report["matchers"].items():
        L.append(f"{m:<14} {r['hit@1']:>6} {r['hit@3']:>6} {r['hit@5']:>6} "
                 f"{r['gold_coverage@5']:>6} {r['gold_coverage@3+traverse']:>11} "
                 f"{r['mean_tokens_top3']:>7} {r['tokens_per_correct@3']:>11}")
    L.append("\nBy shape (hit@3 / hit@5 / cov@5 / cov@3+walk):")
    for m, r in report["matchers"].items():
        L.append(f"  {m}:")
        for sh, s in r["by_shape"].items():
            L.append(f"    {sh:<10} n={s['n']}  hit@3={s['hit@3']}  hit@5={s['hit@5']}  "
                     f"cov@5={s['gold_coverage@5']}  cov@3+walk={s['gold_coverage@3+traverse']}")
    return "\n".join(L)


if __name__ == "__main__":
    rep = evaluate()
    out = fmt(rep)
    print(out)
    open(os.path.join(RESULTS, "summary.txt"), "w").write(out + "\n")
