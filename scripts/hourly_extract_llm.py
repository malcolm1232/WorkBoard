#!/usr/bin/env python3
"""hourly_extractor LLM-dispatch + retry ladder — extracted from hourly_extractor.py (#646, #307 pattern).

The cohesive "turn a chunk of activity into cards via one claude -p call"
concern: the single/multi-bucket extraction call (extract_cards_for_chunk /
extract_cards_for_hour), the failure marker that distinguishes a hard failure
from a legitimately-empty bucket (ChunkExtractionError, #627), and the two-tier
retry ladder that splits/re-buckets a failed chunk (_extract_chunk_with_retries).

Depends ONLY on hourly_common (the digest builder + LLM constants + bucket
helpers) — a leaf, so no circular import back to hourly_extractor.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hourly_common import *  # noqa: E402,F401,F403  (_LLM_PROMPT/_LLM_ARGS/_LLM_ENV/parse_card_array/build_digest/_bucket_hour/_bucket_label)

__all__ = [
    "ChunkExtractionError",
    "extract_cards_for_hour",
    "extract_cards_for_chunk",
    "_extract_chunk_with_retries",
]


# ---------- LLM dispatch --------------------------------------------------

class ChunkExtractionError(Exception):
    """A chunk's LLM extraction genuinely FAILED (subprocess error / non-zero
    exit / non-JSON) — distinct from a bucket that legitimately had no work
    (empty digest → []). #627: without this distinction a failed bucket looked
    identical to an empty one, so the tier-fly marked replay complete and
    silently dropped the bucket's cards. Carries the bucket labels that failed
    so the caller can record/recover them."""
    def __init__(self, labels: str = ""):
        self.labels = labels
        super().__init__(labels)


def extract_cards_for_hour(bucket_events: list[dict], project: Path,
                            bucket_label: str,
                            timeout_s: int = 60) -> list[dict]:
    """Single-bucket extraction (legacy path; --chunk-size 1)."""
    return extract_cards_for_chunk(
        [(bucket_label, bucket_events)], project, timeout_s=timeout_s)


def extract_cards_for_chunk(chunk: list[tuple[str, list[dict]]],
                             project: Path,
                             timeout_s: int = 90) -> list[dict]:
    """Multi-bucket extraction. chunk = [(bucket_label, events), ...] in time
    order. Builds a combined digest with bucket headers, sends ONE LLM call,
    returns a flat card array. Pays the claude -p cold-start once per chunk
    instead of per bucket."""
    sections: list[str] = []
    seen_heads: set = set()   # #299: cross-bucket dedup within this chunk
    for label, bevents in chunk:
        digest = build_digest(bevents, project, seen_heads=seen_heads)
        if not digest.strip():
            continue
        sections.append(f"=== BUCKET {label} ===\n{digest}")
    if not sections:
        return []
    combined = "\n\n".join(sections)
    full = (
        f"{_LLM_PROMPT}\n\n"
        f"--- WORK ACTIVITY ({len(chunk)} bucket(s), project={project.name}) ---\n"
        f"{combined}\n"
    )
    label_summary = " + ".join(label for label, _ in chunk)
    try:
        proc = subprocess.run(
            _LLM_ARGS,   # shared argv: thinking-off (env) + --strict-mcp-config
            input=full, capture_output=True, text=True, timeout=timeout_s,
            env=_LLM_ENV,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"  ! LLM call failed for chunk [{label_summary}]: {e}",
              file=sys.stderr)
        raise ChunkExtractionError(label_summary) from e
    if proc.returncode != 0:
        print(f"  ! claude -p exit {proc.returncode} for chunk [{label_summary}]",
              file=sys.stderr)
        raise ChunkExtractionError(label_summary)
    cards = parse_card_array(proc.stdout)
    if cards is None:
        print(f"  ! LLM returned non-JSON for chunk [{label_summary}]",
              file=sys.stderr)
        raise ChunkExtractionError(label_summary)
    return cards


def _extract_chunk_with_retries(chunk_keys: list[int],
                                buckets: dict[int, list[dict]],
                                project: Path,
                                bucket_min: int) -> list[dict]:
    """Extract one chunk's cards with the two-tier retry ladder on failure:
    tier 1 splits a multi-bucket chunk in half; tier 2 recursively re-buckets
    a single failed bucket at half bucket_min.

    #627: failure is now signalled by ChunkExtractionError (raised by
    extract_cards_for_chunk), NOT by an empty list — an empty list means the
    bucket legitimately had no work and is returned as-is (no wasted retry). If
    the whole ladder exhausts on a HARD failure and recovers nothing, this
    re-raises ChunkExtractionError so the caller can record the dropped bucket
    instead of mistaking it for an empty one."""

    def _retry_recursive_subbuckets(
            bucket_events: list[dict], current_min: int,
            depth: int = 0, max_depth: int = 3) -> list[dict]:
        """Last-resort: a single bucket of `current_min` minutes failed. Re-bucket
        its events at half the size and retry each sub-bucket. Recurse until
        success or max_depth. Best-effort: returns whatever it recovers (possibly
        []); never raises — the caller decides whether [] here is a hard failure."""
        if not bucket_events or current_min <= 1 or depth >= max_depth:
            return []
        half_min = max(1, current_min // 2)
        sub_buckets: dict[int, list[dict]] = {}
        for ev in bucket_events:
            sub_buckets.setdefault(
                _bucket_hour(ev["ts"], half_min), []).append(ev)
        sub_keys = sorted(sub_buckets.keys())
        print(f"  ↻↻ recursive retry depth={depth+1}: "
              f"re-bucket {len(bucket_events)} events at {half_min}min "
              f"→ {len(sub_keys)} sub-bucket(s)", file=sys.stderr)
        recovered: list[dict] = []
        for sk in sub_keys:
            label = _bucket_label(sk, half_min)
            try:
                cards = extract_cards_for_chunk(
                    [(label, sub_buckets[sk])], project)
            except ChunkExtractionError:
                cards = _retry_recursive_subbuckets(
                    sub_buckets[sk], half_min, depth + 1, max_depth)
            recovered.extend(cards)
        return recovered

    chunk = [(_bucket_label(k, bucket_min), buckets[k])
             for k in chunk_keys]
    chunk_label = ", ".join(_bucket_label(k, bucket_min) for k in chunk_keys)
    try:
        # Clean result (cards OR a legitimate empty bucket) — no retry needed.
        return extract_cards_for_chunk(chunk, project)
    except ChunkExtractionError:
        pass  # hard failure → drop into the retry ladder below

    # Tier 1 retry: chunk-size > 1 → split in half (smaller LLM digests).
    if len(chunk_keys) > 1:
        mid = len(chunk_keys) // 2
        halves = [chunk_keys[:mid], chunk_keys[mid:]]
        print(f"  ↻ retry: splitting failed chunk [{chunk_label}] "
              f"into {len(halves)} halves", file=sys.stderr)
        recovered: list[dict] = []
        hard_fail = False
        for half in halves:
            sub_chunk = [(_bucket_label(k, bucket_min), buckets[k])
                         for k in half]
            try:
                sub_cards = extract_cards_for_chunk(sub_chunk, project)
            except ChunkExtractionError:
                # Tier 2 retry: a single failed bucket → recursive sub-bucketing.
                # A multi-bucket half that still fails isn't sub-split (matches
                # the original ladder) — it counts as a hard failure if empty.
                sub_cards = (_retry_recursive_subbuckets(buckets[half[0]],
                                                         bucket_min)
                             if len(half) == 1 else [])
                if not sub_cards:
                    hard_fail = True
            recovered.extend(sub_cards)
        # Only a hard failure that recovered NOTHING is a genuine drop. A partial
        # recovery keeps what it got (better than dropping the whole chunk).
        if hard_fail and not recovered:
            raise ChunkExtractionError(chunk_label)
        return recovered

    # Single-bucket chunk failed from the start → tier 2 recursive sub-bucketing.
    cards = _retry_recursive_subbuckets(buckets[chunk_keys[0]], bucket_min)
    if not cards:
        raise ChunkExtractionError(chunk_label)
    return cards
