#!/usr/bin/env python3
"""Unit tests for the SHIPPED matcher (scripts/text_search.py), run against the
FROZEN snapshot — isolated + reproducible, never touches the live board.

    python3 test_text_search.py    # prints PASS/FAIL, exits non-zero on failure
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import text_search as ts  # the SHIPPED module

CARDS = json.load(open(os.path.join(HERE, "inputs", "board_snapshot.json")))["cards"]

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


print("text_search.py unit tests (frozen snapshot, N=%d cards)" % len(CARDS))

# 1. Explicit #ref resolves deterministically to that exact card (top-1).
top = ts.rank("what's the status of #503", CARDS, top=3)
check("#503 ref resolves to card 503 at rank-1", top and top[0][1]["num"] == 503)

# 2. Pinpoint literal (commit-ish word) surfaces the bug card.
top = ts.rank("the silent-drop bootstrap bug #627", CARDS, top=3)
check("#627 surfaces card 627 in top-3", any(c["num"] == 627 for _, c in top))

# 3. Determinism: identical query → identical ranking.
a = [c["num"] for _, c in ts.rank("multi-board concurrency hardening", CARDS, top=5)]
b = [c["num"] for _, c in ts.rank("multi-board concurrency hardening", CARDS, top=5)]
check("deterministic (same query → same order)", a == b)

# 4. Silence: empty / pure-stopword query returns nothing (no false recall).
check("empty query is silent", ts.rank("", CARDS) == [])
check("pure-stopword query is silent", ts.rank("what did we do again", CARDS) == [])

# 5. min_score gate: a nonsense token returns nothing.
check("nonsense query is silent", ts.rank("zzqxblargwobble", CARDS) == [])

# 6. Topical recall: a keyword query lands the right card in top-3.
top = ts.rank("pulsating cards cannot drag", CARDS, top=3)
check("topical query surfaces drag/pulse card #503 top-3", any(c["num"] == 503 for _, c in top))

# 7. Graph traversal: expand_links reaches a linked neighbour.
linked = [c for c in CARDS if c.get("linkedCards")]
if linked:
    seed = linked[0]["num"]
    nbrs = ts.expand_links([seed], CARDS, hops=1)
    check("expand_links includes the seed + ≥1 neighbour", seed in nbrs and len(nbrs) >= 2)
else:
    check("expand_links smoke (no links in snapshot)", True)

# 8. Score is a finite float and ordering is by score desc.
top = ts.rank("reconcile stale sweep backlog", CARDS, top=5)
scores = [s for s, _ in top]
check("scores sorted descending", scores == sorted(scores, reverse=True))

# 9. score() scores the TARGET card even when it isn't in the passed corpus
#    (regression: used to fall back to index 0 and score the wrong card).
tgt = {"num": 99999, "title": "unique zebra giraffe", "origin": "", "tags": [], "subtasks": []}
others = CARDS[:3]
check("score() scores target (not corpus[0]) when absent from cards",
      ts.score("zebra giraffe", tgt, cards=others) > 0
      and ts.score("nonmatching words here", tgt, cards=others) == 0)

# 10. --traverse path: a numless linked card never enters expand_links (regression:
#     a None num crashed the later sort in cmd_recall).
mixed = [{"num": 1, "id": "a", "title": "A", "linkedCards": ["b", "ghost"], "tags": [], "subtasks": []},
         {"num": 2, "id": "b", "title": "B", "tags": [], "subtasks": []},
         {"num": None, "id": "ghost", "title": "numless", "tags": [], "subtasks": []}]
ext = ts.expand_links([1], mixed, hops=1)
check("expand_links omits numless linked cards (no None)", None not in ext and 2 in ext)

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL PASS")
