#!/usr/bin/env python3
"""Regenerate board/index.json — compact one-line-per-card digest.

The Steward reads index.json on every invocation instead of the full board.json.
For a 65-card board: ~5KB digest vs ~50KB full = ~10x context savings on startup.

Usage:  regen_index.py /path/to/board/board.json
        regen_index.py board/board.json   # relative ok
"""
import json, sys, datetime
from pathlib import Path
from collections import Counter

SNIPPET_CHARS = 140


def _snippet(s, n=SNIPPET_CHARS):
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _count_subtasks(nodes):
    total = done = 0
    for n in nodes or []:
        total += 1
        if n.get("done"):
            done += 1
        ct, cd = _count_subtasks(n.get("children"))
        total += ct
        done += cd
    return total, done


def build_index(board):
    cards = board.get("cards", [])
    idx_cards = []
    for c in cards:
        total, done = _count_subtasks(c.get("subtasks"))
        idx_cards.append({
            "n": c.get("num"),
            "id": c.get("id"),
            "code": c.get("code") or "",
            "title": c.get("title") or "",
            "col": c.get("column"),
            "prio": c.get("priority"),
            "upd": c.get("updatedAt"),
            "done": c.get("doneAt"),
            "tags": c.get("tags") or [],
            "p": f"{done}/{total}" if total else "",
            "links": len(c.get("linkedCards") or []),
            "origin": _snippet(c.get("origin")),
        })
    col_counts = Counter(c.get("column", "?") for c in cards)
    return {
        "rev": board.get("rev"),
        "generatedAt": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totalCards": len(cards),
        "columns": [{"id": k, "count": v} for k, v in col_counts.most_common()],
        "cards": idx_cards,
    }


def main():
    if len(sys.argv) < 2:
        print("usage: regen_index.py <path-to-board.json>", file=sys.stderr)
        sys.exit(2)
    p = Path(sys.argv[1]).expanduser().resolve()
    if not p.is_file():
        print(f"not found: {p}", file=sys.stderr)
        sys.exit(1)
    board = json.loads(p.read_text())
    idx = build_index(board)
    out = p.parent / "index.json"
    out.write_text(json.dumps(idx, indent=2, ensure_ascii=False) + "\n")
    size_kb = out.stat().st_size / 1024
    print(f"wrote {out} — {idx['totalCards']} cards, rev {idx['rev']}, {size_kb:.1f}KB")


if __name__ == "__main__":
    main()
