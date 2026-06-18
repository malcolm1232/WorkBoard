"""WorkBoard adapter for the 2026-06 study.

Two surfaces, mirroring the uniform peer interface:

  ingest_estimate(corpus_dir)  -> dict   (Study A — bootstrap cost, deterministic, no model)
  recall(query)                -> dict   (headline — real, against the LIVE board)

DESIGN NOTES
------------
Ingest: the bootstrap path (scripts/hourly_extractor.py) harvests transcripts
from `Path.home()/.claude/projects`. We isolate the corpus by pointing HOME at
a sandbox whose .claude/projects holds ONLY the fixture transcripts, then call
the extractor's own harvest/bucketize functions as a library to count buckets,
chunks, and per-bucket digest tokens (the exact text Haiku would see) WITHOUT
making a single model call. Output-side tokens (the cards) are taken from a real
tiny run; for medium/large they scale by tiny's measured cards-per-bucket ratio.

Recall: the live board ALREADY contains cards #582-730 covering the May-June
corpus window — it IS WorkBoard's memory artifact for that period. So recall is
measured for real, with zero model calls, as the full two-layer retrieval chain
a real session pays:

  index layer  : `card.py list` grepped for the query keywords (~38 tok/line)
  detail layer : `card.py show <n>` for each card the answer needs

This is the SAME two-layer shape claude-mem documents (search -> get_observations),
so the comparison is apples-to-apples.
"""

from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
# STANDALONE: never import/execute from the live product tree. We use a READ-ONLY
# COPY of the product scripts (lib/product_scripts_ro/), so running the study can
# never touch ~/Desktop/WorkBoard. Re-copy deliberately with rsync if the product
# extraction code changes (see README).
SCRIPTS = BENCH_DIR / "lib" / "product_scripts_ro"
CARD = SCRIPTS / "card.py"

# SAFETY: the study NEVER reads or writes the LIVE board. Recall runs entirely
# against a frozen local COPY (board_snapshot.json) so the harness cannot
# interfere with the running product, and numbers don't drift as the live board
# changes. Re-snapshot deliberately:
#   cp board/board.json docs/study_2026_06/board_snapshot.json
SNAPSHOT = BENCH_DIR / "board_snapshot.json"

sys.path.insert(0, str(BENCH_DIR))
import tokencount  # noqa: E402

# Stopwords stripped from a query before keyword-grepping the index.
_STOP = set("the a an of to in on and or for what which is are was were did do "
            "does how why when where who whats what's around still open from "
            "into vs version card number commit file path state story trace".split())


def _keywords(q: str) -> list[str]:
    toks = re.findall(r"#?\w[\w.-]*", q.lower())
    out = []
    for t in toks:
        if t in _STOP or (len(t) < 2 and not t.startswith("#")):
            continue
        out.append(t)
    return out


# --------------------------------------------------------------------------
# Board content index — fact string -> cards that literally contain it.
# This makes "correct" mean the gold fact is REALLY in the retrieved card
# text, not a keyword-match proxy. Built once from the live board.json.
# --------------------------------------------------------------------------
def _all_cards(o):
    if isinstance(o, dict):
        if "writeup" in o and "num" in o:
            yield o
        for v in o.values():
            yield from _all_cards(v)
    elif isinstance(o, list):
        for v in o:
            yield from _all_cards(v)


_BOARD_CARDS: list[dict] | None = None


def _board_cards() -> list[dict]:
    global _BOARD_CARDS
    if _BOARD_CARDS is None:
        b = json.loads(SNAPSHOT.read_text())
        _BOARD_CARDS = list(_all_cards(b))
    return _BOARD_CARDS


def _cards_containing(fact: str) -> list[int]:
    """Card numbers whose content covers `fact`.

    A gold fact of the form "#N" is an EXACT card reference — it resolves to
    card N only (if it exists), never to other cards that merely mention the
    digits N in prose. All other facts (shas, versions, file paths, phrases)
    resolve by literal substring over title/origin/notes/writeup."""
    if re.fullmatch(r"#\d+", fact):
        n = int(fact[1:])
        return [n] if any(c.get("num") == n for c in _board_cards()) else []
    fl = fact.lower()
    hits = []
    for c in _board_cards():
        blob = " ".join(str(c.get(k, "")) for k in ("title", "origin", "notes", "writeup")).lower()
        if fl in blob:
            hits.append(c["num"])
    return sorted(set(hits))


def resolve_answer_cards(gold_ids: list[str]) -> tuple[list[int], list[str]]:
    """Minimal set of cards whose combined content covers every gold fact.
    Returns (answer_card_nums, facts_not_in_any_card). Greedy set-cover so we
    don't over-fetch: prefer cards that cover the most still-uncovered facts."""
    fact_to_cards = {f: set(_cards_containing(f)) for f in gold_ids}
    uncovered = {f for f, cs in fact_to_cards.items() if cs}
    misses = [f for f, cs in fact_to_cards.items() if not cs]
    chosen: list[int] = []
    while uncovered:
        # pick the card covering the most uncovered facts
        tally: dict[int, int] = {}
        for f in uncovered:
            for c in fact_to_cards[f]:
                tally[c] = tally.get(c, 0) + 1
        if not tally:
            break
        best = max(tally, key=lambda c: (tally[c], -c))
        chosen.append(best)
        uncovered = {f for f in uncovered if best not in fact_to_cards[f]}
    return sorted(set(chosen)), misses


# --------------------------------------------------------------------------
# Recall  (real, against the frozen board snapshot)
# --------------------------------------------------------------------------
def _card_by_num(num: int) -> dict | None:
    for c in _board_cards():
        if c.get("num") == num:
            return c
    return None


def _compact_card(c: dict) -> str:
    """The minimal recall PAYLOAD of a card: the human-meaningful content that
    actually answers a query — title, column, origin, notes, writeup, subtask
    texts, links. Excludes internal metadata (full history array with per-move
    timestamps, subtask ids/doneAt, card id) that a recall never needs. This is
    the fair analogue of claude-mem's get_observations returning observation
    TEXT, not its internal row metadata."""
    links = [l.get("num") if isinstance(l, dict) else l
             for l in (c.get("linkedCards") or [])]
    return json.dumps({
        "num": c.get("num"),
        "title": c.get("title"),
        "column": c.get("column"),
        "origin": c.get("origin"),
        "notes": c.get("notes"),
        "writeup": c.get("writeup"),
        "subtasks": [s.get("text") for s in (c.get("subtasks") or [])],
        "links": links,
    }, ensure_ascii=False)


def _card_detail_tokens(num) -> tuple[int, int]:
    """(compact_tokens, full_show_tokens) for one card. compact = fair recall
    payload (headline); full = raw `card.py show` JSON (reported for transparency)."""
    n = int(str(num).lstrip("#"))
    c = _card_by_num(n)
    compact = tokencount.count(_compact_card(c)) if c else 0
    try:
        out = subprocess.run([sys.executable, str(CARD), "--board", str(SNAPSHOT), "show", str(n)],
                             capture_output=True, text=True, timeout=30)
        full = tokencount.count(out.stdout) if out.returncode == 0 else 0
    except Exception:
        full = 0
    return compact, full


_LIST_CACHE: list[str] | None = None


def _list_lines() -> list[str]:
    global _LIST_CACHE
    if _LIST_CACHE is None:
        out = subprocess.run([sys.executable, str(CARD), "--board", str(SNAPSHOT), "list"],
                             capture_output=True, text=True, timeout=60)
        _LIST_CACHE = out.stdout.splitlines()
    return _LIST_CACHE


def _index_layer_tokens(query: str) -> tuple[int, int]:
    """Cost of finding the candidate cards: grep the compact `card.py list`
    index for the query keywords. Returns (tokens_of_matching_lines, n_lines).
    A real session pays for the matching lines it reads, not the whole board."""
    kws = _keywords(query)
    lines = _list_lines()
    matched = []
    for ln in lines:
        low = ln.lower()
        if any(k.lstrip("#") in low for k in kws):
            matched.append(ln)
    # Cap at a realistic scan window — a human/Claude skims the top matches.
    matched = matched[:25]
    text = "\n".join(matched)
    return tokencount.count(text), len(matched)


def recall(query: dict) -> dict:
    """Measure the real two-layer retrieval cost to answer one query.

    Detail set = the minimal set of cards whose literal content covers every
    gold fact (greedy set-cover via resolve_answer_cards). WorkBoard pays its
    real per-card `show` tokens. found_gold is True iff every gold fact is
    actually present in some card's content (a real correctness check, not a
    proxy). Facts that live only in memory files / not on any card are listed
    in `board_misses` — honest board-coverage gaps."""
    gold_ids = query.get("gold_ids", [])
    answer_cards, misses = resolve_answer_cards(gold_ids)
    idx_tokens, idx_lines = _index_layer_tokens(query["q"])

    detail_tokens = 0          # headline: compact recall payload
    detail_tokens_full = 0     # transparency: raw `card.py show` JSON
    fetched = 0
    for c in answer_cards:
        compact, full = _card_detail_tokens(c)
        if compact == 0 and full == 0:
            continue
        detail_tokens += compact
        detail_tokens_full += full
        fetched += 1

    found = (len(misses) == 0 and fetched > 0)
    return {
        "system": "workboard",
        "id": query["id"],
        "shape": query["shape"],
        "index_tokens": idx_tokens,
        "index_lines": idx_lines,
        "detail_tokens": detail_tokens,
        "detail_tokens_full_show": detail_tokens_full,
        "detail_units": fetched,
        "answer_cards": answer_cards,
        "total_tokens": idx_tokens + detail_tokens,
        "found_gold": found,
        "board_misses": misses,
    }


# --------------------------------------------------------------------------
# Ingest estimate  (Study A — deterministic, no model calls)
# --------------------------------------------------------------------------
def _sandbox_home(corpus_dir: Path) -> Path:
    """Build a throwaway HOME whose .claude/projects holds only the fixture."""
    home = BENCH_DIR / "peers" / "_wb_ingest_home"
    proj = home / ".claude" / "projects" / "-Users-malco"
    if proj.exists():
        shutil.rmtree(home)
    proj.mkdir(parents=True)
    src = corpus_dir / "transcripts"
    for f in src.glob("*.jsonl"):
        # symlink — read-only harvest, no need to copy hundreds of MB
        (proj / f.name).symlink_to(f.resolve())
    return home


def ingest_estimate(corpus_dir: Path, days: int = 90) -> dict:
    """Count buckets, chunks (=model calls), and INPUT tokens (per-bucket
    digests) the bootstrap would feed Haiku — without calling Haiku."""
    corpus_dir = Path(corpus_dir)
    home = _sandbox_home(corpus_dir)
    proj = home / ".claude" / "projects" / "-Users-malco"

    env_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        sys.path.insert(0, str(SCRIPTS))
        # Re-import fresh so Path.home() picks up the new HOME inside helpers
        import importlib
        import hourly_extractor as hx
        importlib.reload(hx)
        import hourly_common as hc
        importlib.reload(hc)

        days_eff = max(days, 1)
        events = hx._flatten_events(proj, days_eff, sources={"jsonl"})
        scoped = hx._filter_events(events, proj, None, 0, seed_if_empty=False) if events else []
        input_tokens = 0
        buckets = 0
        chunks = 0
        if scoped:
            sorted_keys, buckets_by_key, chunk_list = hx._bucketize(scoped, 60, True, 0, 1)
            buckets = len(sorted_keys)
            chunks = len(chunk_list)
            for key in sorted_keys:
                digest = hc.build_digest(buckets_by_key[key], proj)
                input_tokens += tokencount.count(digest)
    finally:
        if env_home is not None:
            os.environ["HOME"] = env_home

    return {
        "system": "workboard",
        "corpus": corpus_dir.name,
        "events": len(scoped) if scoped else 0,
        "buckets": buckets,
        "model_calls": chunks,            # one Haiku call per chunk (chunk-size 1)
        "ingest_input_tokens": input_tokens,
        "sandbox_home": str(home),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", type=Path, help="corpus dir to estimate")
    ap.add_argument("--recall-all", action="store_true")
    args = ap.parse_args()
    if args.ingest:
        print(json.dumps(ingest_estimate(args.ingest), indent=2))
    if args.recall_all:
        queries = json.loads((BENCH_DIR / "queries.json").read_text())["queries"]
        for q in queries:
            print(json.dumps(recall(q)))
