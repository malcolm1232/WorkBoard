"""Recall driver — tokens-to-recall, WorkBoard (real) vs mem0 (spec).

For each of the 20 frozen queries:
  - WorkBoard: real two-layer retrieval against the frozen board snapshot
    (index = grep of `card.py list`; detail = compact card payload).
  - mem0: modeled from its own published numbers — a flat ~1.8K injected memory
    bundle per query (its best case; independent of answer fan-out).

Correctness: WorkBoard found_gold is a REAL check (every gold fact literally
present in a fetched card). mem0 is assumed to retrieve successfully for every
query (optimistic for it). board-misses (WB's board can't answer but mem0's
vector store likely can) are flagged as honest mem0 wins.

Writes results/raw/recall.json. No model calls; reads only the frozen snapshot.
"""

from __future__ import annotations
import json
import statistics
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(BENCH_DIR / "peers"))
import workboard_adapter as wb       # noqa: E402
import mem0_adapter as m0            # noqa: E402
import tokencount                    # noqa: E402

RESULTS = BENCH_DIR / "results"
RAW = RESULTS / "raw"


def run() -> dict:
    queries = json.loads((BENCH_DIR / "queries.json").read_text())["queries"]
    rows = []
    for q in queries:
        w = wb.recall(q)
        n_units = w["detail_units"] if w["detail_units"] else 1
        m = m0.recall(q, n_units=n_units)
        rows.append({
            "id": q["id"],
            "shape": q["shape"],
            "wb_index": w["index_tokens"],
            "wb_detail": w["detail_tokens"],
            "wb_detail_full_show": w["detail_tokens_full_show"],
            "wb_total": w["total_tokens"],
            "wb_units": w["detail_units"],
            "wb_found": w["found_gold"],
            "wb_board_miss": bool(w["board_misses"]),
            "m0_total": m["total_tokens"],
            "answer_cards": w["answer_cards"],
        })

    def agg(subset):
        if not subset:
            return None
        wbt = [r["wb_total"] for r in subset]
        m0t = [r["m0_total"] for r in subset]
        wmean, mmean = statistics.mean(wbt), statistics.mean(m0t)
        return {
            "n": len(subset),
            "wb_mean_total": round(wmean, 1),
            "m0_mean_total": round(mmean, 1),
            # positive = WB lighter; mem0's flat bundle is usually leaner per query,
            # so this is typically negative — reported honestly.
            "reduction_vs_mem0_pct": round((1 - wmean / mmean) * 100, 1) if mmean else None,
            "wb_wins_vs_mem0": sum(1 for r in subset if r["wb_total"] < r["m0_total"]),
        }

    found = [r for r in rows if r["wb_found"]]
    by_shape = {sh: agg([r for r in found if r["shape"] == sh])
                for sh in ("pinpoint", "thematic", "lifecycle")}
    out = {
        "tokenizer": tokencount.backend_name(),
        "m0_params": m0.DEFAULTS,
        "rows": rows,
        "aggregate_found_only": agg(found),
        "by_shape_found_only": by_shape,
        "board_misses": [r["id"] for r in rows if r["wb_board_miss"]],
        "found_count": len(found),
        "total_queries": len(rows),
    }
    RESULTS.mkdir(exist_ok=True)
    RAW.mkdir(exist_ok=True)
    (RAW / "recall.json").write_text(json.dumps(out, indent=2))
    return out


def _print(out: dict):
    print(f"tokenizer: {out['tokenizer']}")
    print(f"{'id':4} {'shape':9} {'WB_idx':>6} {'WB_det':>6} {'WB_tot':>6}  {'mem0':>6}  winner")
    for r in out["rows"]:
        win = "WB" if (r["wb_found"] and r["wb_total"] < r["m0_total"]) else "mem0"
        if not r["wb_found"]:
            win = "mem0(board-miss)"
        print(f"{r['id']:4} {r['shape']:9} {r['wb_index']:6} {r['wb_detail']:6} "
              f"{r['wb_total']:6}  {r['m0_total']:6}  {win}")
    a = out["aggregate_found_only"]
    print(f"\nFOUND-ONLY ({a['n']} queries WorkBoard can answer):")
    print(f"  WorkBoard mean : {a['wb_mean_total']:.0f} tok")
    print(f"  mem0 mean      : {a['m0_mean_total']:.0f} tok  "
          f"(WB {a['reduction_vs_mem0_pct']}% vs mem0; WB wins {a['wb_wins_vs_mem0']}/{a['n']})")
    for sh, s in out["by_shape_found_only"].items():
        if s:
            print(f"  {sh:9}: WB {s['wb_mean_total']:.0f} | mem0 {s['m0_mean_total']:.0f} "
                  f"({s['reduction_vs_mem0_pct']}%)")
    print(f"  board-misses (mem0 likely wins): {out['board_misses']}")


if __name__ == "__main__":
    _print(run())
