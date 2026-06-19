"""Zero-dependency candidate matchers for the WorkBoard retrieval bake-off.

Each matcher ranks the board's INDEX LAYER (one compact record per card:
title + code + tags + 140-char origin snippet — exactly what `board/index.json`
exposes and what the recall CLI would search) against a free-text query and
returns card numbers best→worst.

Three matchers of increasing sophistication, all stdlib-only (no vector DB, no
API, no model call) — that "no infra" property is the whole WorkBoard thesis:

  H1  lexical      — literal/keyword overlap + difflib ratio (no corpus stats)
  H2  bm25         — Okapi BM25 over the card corpus (IDF + length norm)
  H3  tfidf_cosine — TF-IDF vector cosine (a zero-dep stand-in for the peers'
                     dense-vector cosine, so we can see what a "vector-shaped"
                     ranker does on THIS corpus without an embedding API)

Tokenisation deliberately PRESERVES the literals that pinpoint queries hinge on
(`#627`, commit shas, `board.html`, `v0.9.21`) because that is exactly the
signal dense embeddings blur — see the peer algorithm studies.
"""
from __future__ import annotations

import math
import re
import difflib
from collections import Counter

# Stopwords: reuse the spirit of scripts/need_detect.py — strip connective/query
# scaffolding so topical words dominate the score.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "been", "what", "which", "who", "whom", "whose",
    "how", "when", "where", "why", "did", "do", "does", "done", "we", "i", "you",
    "it", "its", "that", "this", "these", "those", "from", "by", "at", "as", "our",
    "card", "cards", "number", "numbers", "commit", "file", "path", "version",
    "shipped", "still", "open", "around", "during", "story", "state", "trace",
    "changed", "change", "work", "happened", "covered", "cover", "settled",
    "between", "vs", "what's", "whats", "held", "holds", "taken", "before",
}

# A "literal" = a token a pinpoint query pins on and an embedding tends to blur:
#   #627  ·  db9eedd / f93dc43 (7-40 hex)  ·  v0.9.21 / 0.9.34  ·  board.html
_LITERAL_RE = re.compile(
    r"#\d+"                       # card refs
    r"|\b[0-9a-f]{7,40}\b"        # commit shas
    r"|\bv?\d+\.\d+[\w.]*\b"      # versions
    r"|\b[\w./-]+\.[a-z]{2,4}\b"  # file paths / names
)
_WORD_RE = re.compile(r"[a-z][a-z0-9']+")


def literals(text: str) -> set[str]:
    return set(m.lower() for m in _LITERAL_RE.findall(text or ""))


def words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall((text or "").lower())
            if w not in STOPWORDS and len(w) > 1]


# Field weights (BM25F). The TITLE is the human-curated one-line summary written
# FOR future retrieval, so it dominates; writeups are long post-hoc prose, so they
# count but are damped. A local matcher costs ZERO model tokens to search, so we
# index the FULL card (unlike a vector system that must embed+inject) and still
# surface only the thin top-3 titles — the token budget is on what we SURFACE.
FIELD_WEIGHTS = {
    "title": 5.0, "code": 5.0, "tags": 3.0,
    "origin": 2.0, "subtasks": 1.5, "notes": 1.0, "writeup": 0.5,
}


def card_fields(card: dict) -> dict[str, str]:
    return {
        "title": card.get("title", ""),
        "code": card.get("code", ""),
        "tags": " ".join(card.get("tags") or []),
        "origin": card.get("origin") or "",
        "subtasks": " ".join(s.get("text", "") for s in (card.get("subtasks") or [])),
        "notes": card.get("notes") or "",
        "writeup": card.get("writeup") or "",
    }


def card_doc(card: dict) -> str:
    """Flat searchable text (used by H1 lexical + H3 tf-idf cosine)."""
    f = card_fields(card)
    return f"#{card['num']} " + " ".join(f.values())


def weighted_tf(card: dict) -> Counter:
    """BM25F field-weighted term frequencies (used by H2)."""
    tf = Counter()
    for field, text in card_fields(card).items():
        w = FIELD_WEIGHTS[field]
        for t in words(text):
            tf[t] += w
    return tf


# ---------------------------------------------------------------------------
# Corpus statistics (shared by H2/H3), built once per matcher instance.
# ---------------------------------------------------------------------------
class Corpus:
    def __init__(self, cards: list[dict]):
        self.cards = cards
        self.docs_words = []       # per-card token list (flat, for H1/H3)
        self.docs_wtf = []         # per-card BM25F weighted term freqs (H2)
        self.docs_literals = []    # per-card literal set
        self.titles = []
        self.num_index = {c["num"]: i for i, c in enumerate(cards)}
        df = Counter()
        for c in cards:
            doc = card_doc(c)
            ws = words(doc)
            self.docs_words.append(ws)
            self.docs_wtf.append(weighted_tf(c))
            self.docs_literals.append(literals(doc))
            self.titles.append(c["title"])
            for t in set(ws):
                df[t] += 1
        self.N = len(cards)
        self.df = df
        self.avgdl = sum(len(w) for w in self.docs_words) / max(self.N, 1)
        self.avgdl_w = sum(sum(wtf.values()) for wtf in self.docs_wtf) / max(self.N, 1)
        # idf for tf-idf (smoothed) and bm25 (Robertson/Sparck-Jones)
        self.idf_tfidf = {t: math.log((self.N + 1) / (n + 1)) + 1.0 for t, n in df.items()}
        self.idf_bm25 = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}


def _literal_bonus(q_lits: set[str], d_lits: set[str]) -> float:
    """Exact literal overlap — the deterministic edge on pinpoint queries.
    A query that names `#627` resolves to card 627 with zero ambiguity; a dense
    vector embeds `#627` into a fuzzy point and blurs it against neighbours."""
    return float(len(q_lits & d_lits))


def rank(matcher: str, corpus: Corpus, query: str, k: int | None = None) -> list[int]:
    q_words = words(query)
    q_lits = literals(query)
    q_wc = Counter(q_words)
    # Explicit #ref resolution: a query that NAMES #627 deterministically resolves
    # to card 627 — the structured edge a dense vector cannot match (it blurs the
    # literal). Give the named card a decisive bonus so it always surfaces.
    named = {int(t[1:]) for t in q_lits if re.fullmatch(r"#\d+", t)}
    scores = []
    for i, c in enumerate(corpus.cards):
        dwords = corpus.docs_words[i]
        dlits = corpus.docs_literals[i]
        lit = _literal_bonus(q_lits, dlits)
        ref_bonus = 10.0 if c["num"] in named else 0.0
        if matcher == "lexical":
            # H1: raw overlap + difflib ratio, no corpus stats.
            overlap = len(set(q_words) & set(dwords))
            ratio = difflib.SequenceMatcher(None, query.lower(), corpus.titles[i].lower()).ratio()
            s = 3.0 * lit + 1.0 * overlap + 0.5 * ratio + ref_bonus
        elif matcher == "bm25":
            # H2: BM25F (field-weighted, k1=1.5, b=0.75) + literal + #ref bonus.
            k1, b = 1.5, 0.75
            wtf = corpus.docs_wtf[i]
            dl = sum(wtf.values())
            s = 0.0
            for t in q_wc:
                if t not in wtf:
                    continue
                idf = corpus.idf_bm25.get(t, 0.0)
                num = wtf[t] * (k1 + 1)
                den = wtf[t] + k1 * (1 - b + b * dl / max(corpus.avgdl_w, 1))
                s += idf * num / den
            s += 3.0 * lit + ref_bonus
        elif matcher == "tfidf_cosine":
            # H3: TF-IDF cosine — a zero-dep proxy for dense-vector cosine.
            tf = Counter(dwords)
            dvec = {t: (1 + math.log(tf[t])) * corpus.idf_tfidf.get(t, 0.0) for t in tf}
            qvec = {t: (1 + math.log(q_wc[t])) * corpus.idf_tfidf.get(t, 0.0) for t in q_wc}
            dot = sum(qvec[t] * dvec.get(t, 0.0) for t in qvec)
            dn = math.sqrt(sum(v * v for v in dvec.values())) or 1.0
            qn = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
            s = dot / (dn * qn)
            s += 0.15 * lit + 0.5 * (ref_bonus > 0)  # cosine alone blurs IDs
        else:
            raise ValueError(f"unknown matcher {matcher}")
        scores.append((s, -i, c["num"]))
    scores.sort(reverse=True)
    out = [num for _, _, num in scores]
    return out[:k] if k else out


MATCHERS = ["lexical", "bm25", "tfidf_cosine"]
