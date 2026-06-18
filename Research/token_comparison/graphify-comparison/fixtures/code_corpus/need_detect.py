#!/usr/bin/env python3
"""Shared "does this name MORE THAN ONE thing?" heuristic — the single source of
truth for board-steward's multi-part / multi-need detection (#562).

Three consumers read the SAME signals, so the definition never drifts:
  • card_commands._looks_multipart  → the #103 decompose-before-IP guard (card
    title/origin shaped).
  • hook_user_prompt.sh             → the proactive multi-need prompt nudge
    (free-form user prompt shaped).
  • _hook_stop_recon.py             → the non-blocking sign-off mirror (counts
    needs vs cards+subtasks captured).

SHAPE-NEUTRAL by design: these functions only answer "are there multiple needs
here?" / "roughly how many?" — they say NOTHING about whether the right capture
is one card + subtasks or N separate cards. That decision stays entirely with
the header test + Shape table in SKILL.md. Nothing here should ever push toward
multi-card.
"""
from __future__ import annotations

import re

# A numbered/lettered list item: `1. ` / `1) ` at a line/word boundary.
_NUM_RE = re.compile(r"(?:^|\s)(\d+)[.)]\s")
# Explicit list joiners that survive in BOTH card titles and prose.
_AND_JOIN_RE = re.compile(r",\s+and\s+\S")
_SEMI_JOIN_RE = re.compile(r";\s+\S")
# Prose add-on asks ("also …", "and also …", "plus …", "additionally …").
_ALSO_RE = re.compile(r"(?:^|[\n.;]|\band\b)\s*(?:also|additionally|plus)\b")
# Unified need separator for SEGMENT counting (count_needs). Matches any single
# boundary between needs; `re.split` collapses a compound boundary like "; also"
# into ONE split (the blank/short pieces are filtered out), so co-occurring
# signals are NOT double-counted.
_SPLIT_RE = re.compile(
    r"(?:;|,\s+and\b|\balso\b|\badditionally\b|\bplus\b|\s\+\s|(?:^|\s)\d+[.)]\s)")


def _numbered(text: str) -> set:
    return set(_NUM_RE.findall(text))


def looks_multipart_card(title: str, origin: str) -> bool:
    """Title/origin-aware heuristic for the #103 decompose guard. This is the
    EXACT logic that used to live inline in card_commands._looks_multipart —
    behavior preserved verbatim (title-only checks stay title-only):
      • a ` + `-joined title (the canonical multi-part shape)
      • a numbered list with ≥2 items
      • `Header: a, b` — a colon followed by a comma-list (title)
      • ≥2 commas in the title (a list of ≥3 things)
      • an explicit `, and …` / `; …` list joiner anywhere
    """
    title = title or ""
    origin = origin or ""
    text = f"{title}\n{origin}".lower()
    if " + " in title:                       # canonical 2a part separator
        return True
    if len(_numbered(text)) >= 2:
        return True
    if ":" in title and "," in title.split(":", 1)[1]:
        return True
    if title.count(",") >= 2:
        return True
    if _AND_JOIN_RE.search(text) or _SEMI_JOIN_RE.search(text):
        return True
    return False


def looks_multi_need(text: str) -> bool:
    """Does a free-form USER PROMPT name multiple distinct needs? Tuned for
    PROSE, not card titles — deliberately conservative so it doesn't trip on a
    single sentence with incidental commas (that false-positive would nag every
    turn). Fires only on strong list signals:
      • ≥2 numbered/bulleted items
      • a `; ` or `, and ` list joiner
      • ≥2 prose add-on asks ("also …", "plus …", "additionally …")
    """
    t = (text or "").lower()
    if len(_numbered(t)) >= 2:
        return True
    if _AND_JOIN_RE.search(t) or _SEMI_JOIN_RE.search(t):
        return True
    if len(_ALSO_RE.findall(t)) >= 2:
        return True
    return False


def count_needs(text: str) -> int:
    """Best-effort, CONSERVATIVE estimate of the number of distinct needs in a
    prompt — by SEGMENT count, not by summing signals (so a compound boundary
    like "; also" counts once, not twice). The sign-off mirror compares this to
    the cards+subtasks captured, so over-counting would false-flag a complete
    capture; segment-counting + a ≥2-word filter keeps it honest. Returns ≥1.
      • numbered list (≥2 items) → count of distinct item numbers
      • else → number of segments left after splitting on the unified need
        separator, keeping only segments with ≥2 words (drops blanks/fragments
        a compound separator leaves behind)
    """
    t = (text or "").lower()
    nums = _numbered(t)
    if len(nums) >= 2:
        return len(nums)
    segments = [s for s in _SPLIT_RE.split(t) if len(s.split()) >= 2]
    return max(1, len(segments))
