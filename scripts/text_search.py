"""Deterministic card matcher for `card.py recall` (#781).

Score a free-text query ("the auth redirect bug from last week") against every
card and surface the best ENTRY POINTS — the agent/user then `card.py show <#>`
(or follows `linkedCards`) for detail. Stdlib only: no embeddings, no vector DB,
no API, no model call. That "no infra" property is the whole WorkBoard thesis.

The scorer is **BM25F** (field-weighted Okapi BM25) + exact-literal and explicit
`#ref` boosting. It was chosen by a measured bake-off against three zero-dep
candidates (lexical / BM25F / TF-IDF cosine) over 20 gold recall queries on a
frozen 533-card snapshot — BM25F won hit@5 (0.556 overall, **1.0 on pinpoint**),
coverage, and tokens-per-correct (~268 vs mem0's ~6,956 per retrieval). Full
study + reproducible harness: Research/retrieval-bakeoff/ (cards #781/#782).

Why field-weighting: a card's TITLE is the human-curated one-line summary written
*for* future retrieval, so it dominates; the post-hoc writeup counts but is damped.
A local matcher costs ZERO model tokens to run, so we rank over the card's FULL
text yet surface only the thin top-k titles — the token budget is on what we
SURFACE, never on what we search. Why literal/#ref boosting: a query naming
`#627` / `f93dc43` / `board.html` resolves deterministically — exactly the signal
a dense embedding blurs (and where the vector peers are measurably weakest).

Public API (unchanged, used by card.py recall):
    rank(query, cards, top=3, min_score=...) -> [(score, card), ...]
    score(query, card, cards=None) -> float
    expand_links(seed_nums, cards, hops=1) -> set[int]    # graph traversal
"""
from __future__ import annotations

import math
import re
from collections import Counter

# Connective/query scaffolding stripped so topical words dominate.
_STOP = set(
    "the a an of to in on at and or for is are was were be been do does did done "
    "how why what which when where who whom that this these those it its my your our "
    "i we you he she they last time ago about with from into vs over under as by plus "
    "remember rmb recall find search show me again still open close card cards number "
    "numbers commit file path version shipped story state trace changed change work "
    "happened covered cover settled between held holds taken before thing things stuff".split()
)

# Literals a pinpoint query pins on and an embedding blurs:
#   #627  ·  db9eedd (7-40 hex)  ·  v0.9.21 / 0.9.34  ·  board.html
_LITERAL_RE = re.compile(
    r"#\d+|\b[0-9a-f]{7,40}\b|\bv?\d+\.\d+[\w.]*\b|\b[\w./-]+\.[a-z]{2,4}\b")
_WORD_RE = re.compile(r"[a-z][a-z0-9']+")

# BM25F field weights. Title/code/tags are curated → high; writeup is long prose → damped.
_FIELD_WEIGHTS = {
    "title": 5.0, "code": 5.0, "tags": 3.0,
    "origin": 2.0, "subtasks": 1.5, "notes": 1.0, "writeup": 0.5,
}
_K1, _B = 1.5, 0.75


def _literals(text: str) -> set:
    return {m.lower() for m in _LITERAL_RE.findall(text or "")}


def _words(text: str) -> list:
    return [w for w in _WORD_RE.findall((text or "").lower())
            if w not in _STOP and len(w) > 1]


def _fields(c: dict) -> dict:
    return {
        "title": c.get("title", "") or "",
        "code": c.get("code", "") or "",
        "tags": " ".join(c.get("tags") or []),
        "origin": c.get("origin") or "",
        "subtasks": " ".join(s.get("text", "") for s in (c.get("subtasks") or [])),
        "notes": c.get("notes") or "",
        "writeup": c.get("writeup") or "",
    }


def _weighted_tf(fields: dict) -> Counter:
    tf = Counter()
    for field, text in fields.items():
        w = _FIELD_WEIGHTS[field]
        for t in _words(text):
            tf[t] += w
    return tf


def _card_literals(num, fields: dict) -> set:
    lits = _literals(" ".join(fields.values()))
    if num is not None:
        lits.add(f"#{num}")
    return lits


def _parse_query(query: str):
    """(qwords, qlits, named) — the single definition of how a query is parsed.
    `named` = explicit card refs (`#627`) for deterministic resolution."""
    qlits = _literals(query)
    named = {int(t[1:]) for t in qlits if t[:1] == "#" and t[1:].isdigit()}
    return Counter(_words(query)), qlits, named


class _Corpus:
    """Corpus stats (IDF, avg doc length) built once per rank() call."""

    def __init__(self, cards: list):
        self.cards = cards
        self.wtf, self.lits = [], []
        for c in cards:
            f = _fields(c)                       # build the 7-field view ONCE per card
            self.wtf.append(_weighted_tf(f))
            self.lits.append(_card_literals(c.get("num"), f))
        df = Counter()
        for wtf in self.wtf:
            for t in wtf:
                df[t] += 1
        self.N = max(len(cards), 1)
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
        self.avgdl = sum(sum(wtf.values()) for wtf in self.wtf) / self.N


def _score_one(qwords: Counter, qlits: set, named: set, corpus: _Corpus, i: int) -> float:
    c = corpus.cards[i]
    wtf = corpus.wtf[i]
    dl = sum(wtf.values())
    s = 0.0
    for t in qwords:
        if t in wtf:
            idf = corpus.idf.get(t, 0.0)
            num = wtf[t] * (_K1 + 1)
            den = wtf[t] + _K1 * (1 - _B + _B * dl / max(corpus.avgdl, 1))
            s += idf * num / den
    s += 3.0 * len(qlits & corpus.lits[i])          # exact literal overlap
    if c.get("num") in named:                       # explicit #ref → decisive
        s += 10.0
    return s


def rank(query: str, cards: list, top: int = 3, min_score: float = 1.0):
    """Return [(score, card), …] for the top matches clearing min_score (stays
    SILENT when nothing is a real match). Sorted by score, then recency."""
    if not cards:
        return []
    qwords, qlits, named = _parse_query(query)
    if not qwords and not qlits:
        return []
    corpus = _Corpus(cards)
    scored = [(_score_one(qwords, qlits, named, corpus, i), c)
              for i, c in enumerate(cards)]
    scored = [(s, c) for s, c in scored if s >= min_score]
    scored.sort(key=lambda x: (-x[0], -(x[1].get("num") or 0)))
    return scored[:top]


def score(query: str, card: dict, cards: list | None = None) -> float:
    """Convenience single-card score. If `cards` (the corpus) is given, uses true
    BM25F IDF over it; otherwise scores `card` as a singleton corpus (literal/#ref
    still apply). card.py recall uses rank(); this is for ad-hoc callers/tests.
    The target card is always placed at index 0 so it's scored regardless of
    whether the passed corpus happens to contain that exact object."""
    others = [c for c in (cards or []) if c is not card]
    corpus = _Corpus([card] + others)
    qwords, qlits, named = _parse_query(query)
    return _score_one(qwords, qlits, named, corpus, 0)


def expand_links(seed_nums, cards: list, hops: int = 1) -> set:
    """Graph traversal: from seed card #s, follow `linkedCards` up to `hops` deep.
    This is how recall reaches a multi-card lifecycle story from a single entry
    point — the structural recall a vector top-k cannot do."""
    # Only map cards that actually have a num, so a numless card can never enter
    # the result set as `None` (which would crash a later sort on the nums).
    id2num = {c.get("id"): c["num"] for c in cards if c.get("num") is not None}
    links = {c["num"]: [id2num[i] for i in (c.get("linkedCards") or []) if i in id2num]
             for c in cards if c.get("num") is not None}
    seen = set(seed_nums)
    frontier = set(seed_nums)
    for _ in range(hops):
        nxt = {m for n in frontier for m in links.get(n, []) if m not in seen}
        seen |= nxt
        frontier = nxt
    return seen
