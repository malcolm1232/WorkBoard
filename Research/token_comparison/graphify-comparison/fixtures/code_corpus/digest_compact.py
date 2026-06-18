#!/usr/bin/env python3
"""digest_compact.py — #299 DIGEST-COMPACT: the token-efficiency layer.

THE single place all digest token-cutting lives, split out of hourly_extractor
so future token work is targetable and the logic is exportable on its own.

Contract: LOSSLESS verbatim compaction — drop only zero-signal boilerplate from
an already-assembled digest, keeping every survivor BYTE-IDENTICAL.

What it drops (and ONLY this):
  - code-fence / bracket artifacts from convo dumps (```json, bare [ ] { })
  - short (≤45-char) no-signal connector heads ("Sync + re-test:", "Done.")
  - consecutive duplicate CLAUDE-head lines
  - cross-bucket-repeated NON-signal heads (pass a shared `seen_heads` set)

HARD GATE — never dropped, even when they look like chatter:
  - USER / COMMIT / CLAUDE-edited / MEMORY / PLAN lines (protected line types)
  - any CLAUDE head carrying a work signal: a backticked `code/file` span, a
    path, a commit sha, a #card-ref, a §/rev/HEAD marker, or a result figure.

Public surface (stable — import these):
  compact_enabled()                  -> bool      (honours DIGEST_COMPACT env)
  head_signal(head: str)             -> bool      (does this head carry signal?)
  head_droppable(head: str)          -> bool      (safe-to-drop boilerplate?)
  compact(lines, seen_heads=None)    -> list[str] (apply all rules, lossless)
  SIGNAL_RE, CODE_ARTIFACT_RE                      (the classifiers themselves)

Stdlib only. No I/O, no deps — so it drops cleanly into any digest pipeline.
"""
from __future__ import annotations

import os
import re

# Toggle: on by default. DIGEST_COMPACT=0 disables (used for A/B measurement).
def compact_enabled() -> bool:
    return os.environ.get("DIGEST_COMPACT", "1") != "0"


# Work-signal tokens. A head matching ANY of these is KEPT regardless of shape.
SIGNAL_RE = re.compile(
    r"`[^`]+`"                                   # `code`/`file` spans
    r"|/[\w./-]+"                                 # path-like tokens
    r"|\b[\w-]+\.(py|md|json|js|html|sh|txt|css|yml|yaml|toml|service|plist|cfg)\b"
    r"|\b[0-9a-f]{7,40}\b"                        # commit shas
    r"|#\d+"                                      # card refs
    r"|§|\brev\b|\bHEAD\b"                        # section / rev / HEAD
    r"|\b\d{2,}\b",                               # counts / result figures / ports
    re.I,
)
# Never-miss keywords — a MIRROR of discover2's MANDATORY_RE + DEFER_RE (kept
# inline so this module stays stdlib-only / dependency-free). A head carrying any
# of these routes a card to the mandatory / notes / backlog columns, so it must
# NEVER be dropped — even a short "must ship X" / "defer Y" with no file or sha.
# (#299 follow-up: the install-time completeness sweep caught such heads slipping
# through the file/sha gate. If you edit MANDATORY_RE/DEFER_RE in discover2, mirror it here.)
NEVER_MISS_RE = re.compile(
    r"\b(must|need to|needs to|gotta|urgent|critical|asap|p0|p1|blocker|"
    r"required|mandatory|cannot ship without|cant ship without|can't ship without"  # MANDATORY_RE
    r"|later|next session|tomorrow|todo|deferred|pending|punt|defer)\b",            # DEFER_RE
    re.I,
)

# Pure convo-dump artifacts (assistant message opened with a JSON block → the
# harvested "head" is just the fence/bracket). Never a work signal.
CODE_ARTIFACT_RE = re.compile(r"^(`{3}\w*|\[\]?|\]|\{\}?|\}|json)$")

# Matches a rendered CLAUDE head line, capturing the head text:  "  [HH:MM:SS] CLAUDE: <head>"
_HEAD_LINE_RE = re.compile(r"\]\s*CLAUDE:\s*(.*)$")


def head_signal(head: str) -> bool:
    """True iff this assistant head carries a work signal (→ must be kept):
    either a file/sha/#ref/§-rev/result figure (SIGNAL_RE) OR a never-miss
    mandatory/defer keyword (NEVER_MISS_RE), so urgency/deferral heads are
    gate-protected even without a file reference."""
    return bool(SIGNAL_RE.search(head) or NEVER_MISS_RE.search(head))


def head_droppable(head: str) -> bool:
    """True iff this assistant head is safe-to-drop boilerplate: NO work signal
    AND (a code-fence artifact OR a short ≤45-char connector). Conservative —
    long lines are KEPT even when they open with 'Now…'/'Stopped…' because they
    routinely continue into a real decision the extractor must see."""
    t = head.strip()
    if not t:
        return True
    if head_signal(t):
        return False
    if CODE_ARTIFACT_RE.match(t):
        return True
    return len(t) <= 45


def detime(line: str) -> str:
    """Strip the leading '  [HH:MM:SS] ' so identical heads at different times
    collapse to one dedup key."""
    return re.sub(r"^\s*\[\d\d:\d\d:\d\d\]\s*", "", line).strip()


def _head_text(line: str) -> str | None:
    """Return the head text if `line` is a CLAUDE head line, else None."""
    m = _HEAD_LINE_RE.search(line)
    return m.group(1) if m else None


def compact(lines: list[str], seen_heads: set | None = None) -> list[str]:
    """Apply lossless compaction to assembled digest lines. Pass a shared
    `seen_heads` set across buckets/chunks to dedup repeated non-signal heads
    end-to-end. No-op (returns lines unchanged) when compaction is disabled.

    Only CLAUDE head lines are ever touched; every other line passes through
    byte-identical, so the hard gate is structurally guaranteed."""
    if not compact_enabled():
        return lines
    out: list[str] = []
    for ln in lines:
        head = _head_text(ln)
        if head is None:
            out.append(ln)               # protected line type — pass through
            continue
        # (1) drop pure-boilerplate heads (no work signal)
        if head_droppable(head):
            continue
        # (2) cross-bucket dedup of repeated NON-signal heads
        if seen_heads is not None and not head_signal(head):
            key = head.strip()
            if key in seen_heads:
                continue
            seen_heads.add(key)
        # (3) collapse a consecutive duplicate head line
        if out and ln == out[-1]:
            continue
        out.append(ln)
    return out
