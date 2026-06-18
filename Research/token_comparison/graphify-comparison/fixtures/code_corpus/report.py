#!/usr/bin/env python3
"""Aggregate telemetry/events.jsonl → a 'where can the Steward improve?' report.

The output is honest pain-first. Not vanity counts. Read top-to-bottom:
issues section tells you what to fix; coverage section tells you what's missing;
behavior section is contextual stats.

Usage:
    python3 report.py                       # all events
    python3 report.py --since 2026-05-25    # since a date (inclusive)
    python3 report.py --days 7              # last N days
    python3 report.py --project /path/...   # filter to one project's board
    python3 report.py --json                # machine-readable instead of markdown
"""
import json, os, sys, datetime, argparse
from pathlib import Path
from collections import Counter, defaultdict

# #378 DE-SPRAWL: must match log_event.py's resolution — the FIXED home dir
# (~/.board-steward/telemetry/), overridable via BOARD_TELEMETRY_FILE.
EVENTS_FILE = Path(os.environ.get("BOARD_TELEMETRY_FILE")
                   or Path.home() / ".board-steward/telemetry/events.jsonl")


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def load_events(since_dt=None, project=None):
    if not EVENTS_FILE.is_file():
        return []
    rows = []
    for line in EVENTS_FILE.open():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since_dt:
            t = _parse_ts(ev.get("ts"))
            if not t or t < since_dt:
                continue
        if project and ev.get("project") != project:
            continue
        rows.append(ev)
    return rows


def render(events, scope_label):
    if not events:
        return f"# Board Steward telemetry — {scope_label}\n\nNo events yet.\n"

    n = len(events)
    triggers = Counter(e.get("trigger", "?") for e in events)
    issues = Counter()
    for e in events:
        for tag in e.get("issues", []) or []:
            issues[tag] += 1

    # Read efficiency: % of invocations that stayed Tier-1 (index only).
    tier1_only = sum(1 for e in events if e.get("reads") == ["index"])
    expanded = sum(1 for e in events if "board" in (e.get("reads") or []))
    touched_archive = sum(1 for e in events if any(r.startswith("archive:") for r in (e.get("reads") or [])))

    # Write activity.
    writes = Counter()
    for e in events:
        for k, v in (e.get("writes") or {}).items():
            writes[k] += int(v or 0)
    read_only = sum(1 for e in events if sum((e.get("writes") or {}).values()) == 0)

    # Bookends.
    greeted = sum(1 for e in events if (e.get("bookends") or {}).get("greeted"))
    signed = sum(1 for e in events if (e.get("bookends") or {}).get("signed_off"))

    # Drift.
    drift_flagged = sum(e.get("drift_flagged", 0) or 0 for e in events)
    drift_applied = sum(e.get("drift_applied", 0) or 0 for e in events)

    # Notes — the gold for "what to improve".
    notes = [(e.get("ts", ""), e.get("trigger", ""), e["notes"].strip())
             for e in events if (e.get("notes") or "").strip()]

    out = []
    out.append(f"# Board Steward telemetry — {scope_label}")
    out.append(f"\n**{n} invocation{'s' if n != 1 else ''}** · first {events[0].get('ts','?')} → last {events[-1].get('ts','?')}\n")

    # === Issues (highest-signal section, always first) ===
    out.append("## 🔴 Issues (rank-ordered — fix these first)")
    if issues:
        for tag, count in issues.most_common():
            pct = 100.0 * count / n
            out.append(f"- **{tag}** — {count}× ({pct:.0f}% of invocations)")
    else:
        out.append("- _no tagged issues yet._")
    out.append("")

    # === Bookend compliance (must be 100%) ===
    out.append("## 🤝 Bookend compliance")
    out.append(f"- Greeted: {greeted}/{n} ({100.0*greeted/n:.0f}%)")
    out.append(f"- Signed off: {signed}/{n} ({100.0*signed/n:.0f}%)")
    if greeted < n or signed < n:
        out.append("  ⚠️  Below 100% — Steward is skipping mandatory bookends. Hard-fix in SKILL.md.")
    out.append("")

    # === Read efficiency ===
    out.append("## 📖 Read efficiency (Tier-1 = index.json only)")
    out.append(f"- Index-only invocations: {tier1_only}/{n} ({100.0*tier1_only/n:.0f}%)")
    out.append(f"- Expanded to full board.json: {expanded}/{n} ({100.0*expanded/n:.0f}%)")
    out.append(f"- Touched archive: {touched_archive}/{n} ({100.0*touched_archive/n:.0f}%)")
    if expanded > n * 0.5:
        out.append("  ⚠️  Expanded >50% of the time — index.json schema may be missing fields the Steward needs.")
    out.append("")

    # === Trigger distribution ===
    out.append("## 🔔 Triggers")
    for t, c in triggers.most_common():
        out.append(f"- {t}: {c}")
    out.append("")

    # === Work done ===
    out.append("## ✍️  Work done")
    out.append(f"- Read-only invocations: {read_only}/{n} ({100.0*read_only/n:.0f}%)")
    out.append(f"- Cards moved: {writes['cards_moved']}")
    out.append(f"- Cards added: {writes['cards_added']}")
    out.append(f"- Subtasks changed: {writes['subtasks_changed']}")
    out.append(f"- Writeups filled: {writes['writeups_filled']}")
    out.append("")

    # === Verb classification (#382) ===
    # Does surfacing bug/improve in the hook shift work off the generic `add`?
    # These events are emitted by card.py's _log_verb_usage on every add/bug/
    # improve. The headline number is the "classified" rate: of all card-
    # creation/reopen actions, how many used a SPECIFIC verb (bug/improve) or a
    # tagged add, vs a plain untyped `add`.
    verb_events = [e for e in events if e.get("trigger") == "card-verb"]
    if verb_events:
        verbs = Counter(e.get("verb", "?") for e in verb_events)
        vtot = len(verb_events)
        adds = [e for e in verb_events if e.get("verb") == "add"]
        tagged_bug_adds = sum(1 for e in adds if e.get("tagged_bug"))
        plain_adds = sum(1 for e in adds
                         if not e.get("tagged_bug") and (e.get("column") != "ideas"))
        classified = vtot - plain_adds   # bug/improve verbs + tagged/idea adds
        out.append("## 🏷️  Verb classification (add vs bug vs improve)")
        for v, c in verbs.most_common():
            out.append(f"- `{v}`: {c} ({100.0*c/vtot:.0f}%)")
        out.append(f"- of `add`s, tagged a bug: {tagged_bug_adds}/{len(adds)}")
        out.append(f"- **classified rate** (specific verb or tag, not a plain task): "
                   f"{classified}/{vtot} ({100.0*classified/vtot:.0f}%)")
        out.append("  _Watch this trend after the hook started naming bug/improve "
                   "(#382): rising = the nudge is working._")
        out.append("")

    # === Drift ===
    out.append("## 🔍 Drift detection")
    out.append(f"- Total drift flagged: {drift_flagged}")
    out.append(f"- Drift applied this same session: {drift_applied}")
    if drift_flagged > 0:
        rate = 100.0 * drift_applied / drift_flagged
        out.append(f"- Apply rate: {rate:.0f}% (low = either user disagrees or Steward over-flags)")
    out.append("")

    # === Pain notes (free-form, the most actionable) ===
    out.append("## 📝 Pain notes (free-form)")
    if notes:
        for ts, trg, n_ in notes[-20:]:  # last 20
            out.append(f"- `{ts[:16]}` [{trg}] {n_}")
    else:
        out.append("- _no notes logged. Add `notes` to events when something feels off — that's the gold._")
    out.append("")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO date — include events on or after this")
    ap.add_argument("--days", type=int, help="last N days only")
    ap.add_argument("--project", help="filter to one project board path")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    args = ap.parse_args()

    since_dt = None
    if args.since:
        since_dt = _parse_ts(args.since)
        if since_dt and since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=datetime.timezone.utc)
    if args.days is not None:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
        since_dt = cutoff if not since_dt else max(since_dt, cutoff)

    events = load_events(since_dt=since_dt, project=args.project)
    label = "all time"
    if args.days is not None:
        label = f"last {args.days}d"
    elif args.since:
        label = f"since {args.since}"
    if args.project:
        label += f" · {args.project}"

    if args.json:
        print(json.dumps({"scope": label, "count": len(events), "events": events}, indent=2, ensure_ascii=False))
    else:
        print(render(events, label))


if __name__ == "__main__":
    main()
