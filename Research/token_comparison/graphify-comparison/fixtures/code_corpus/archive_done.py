#!/usr/bin/env python3
"""Archive Done cards older than N days into board/archive/board-YYYY-MM.json.

Keeps the active board.json small (~15-25 cards) without ever deleting work.
Archives are read only when a #N is explicitly referenced or asked for.

Run at session end. Idempotent — re-running with no new old-Done cards does
nothing and exits silently.

Usage:  archive_done.py /path/to/board/board.json [--days 14] [--dry-run]
"""
import json, sys, datetime, argparse
from pathlib import Path
from collections import defaultdict


def _parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    # Early board entries used date-only strings — normalize to aware UTC midnight.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _iso_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="path to board/board.json")
    ap.add_argument("--days", type=int, default=14, help="archive Done cards older than N days (default 14)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, don't write")
    args = ap.parse_args()

    p = Path(args.path).expanduser().resolve()
    if not p.is_file():
        print(f"not found: {p}", file=sys.stderr)
        sys.exit(1)

    board = json.loads(p.read_text())
    archive_dir = p.parent / "archive"
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)

    to_archive_by_month = defaultdict(list)
    keep = []
    for c in board.get("cards", []):
        done_at = _parse_ts(c.get("doneAt"))
        if c.get("column") == "done" and done_at and done_at < cutoff:
            to_archive_by_month[done_at.strftime("%Y-%m")].append(c)
        else:
            keep.append(c)

    archived_count = sum(len(v) for v in to_archive_by_month.values())
    if not archived_count:
        print(f"nothing to archive (cutoff: Done older than {args.days}d)")
        return

    if args.dry_run:
        print(f"[dry-run] would archive {archived_count} cards:")
        for mkey, cards in sorted(to_archive_by_month.items()):
            print(f"  {mkey} ({len(cards)}):")
            for c in cards:
                print(f"    #{c.get('num')} [{c.get('code') or '-'}] {c.get('title','')[:60]}")
        return

    archive_dir.mkdir(exist_ok=True)
    for mkey, cards in to_archive_by_month.items():
        afp = archive_dir / f"board-{mkey}.json"
        if afp.exists():
            existing = json.loads(afp.read_text())
            existing_ids = {c.get("id") for c in existing.get("cards", [])}
            for c in cards:
                if c.get("id") not in existing_ids:
                    existing["cards"].append(c)
            existing["archivedAt"] = _iso_now()
        else:
            existing = {
                "archivedAt": _iso_now(),
                "monthKey": mkey,
                "cards": cards,
            }
        afp.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
        print(f"archived {len(cards)} cards → {afp.name}")

    board["cards"] = keep
    board["rev"] = board.get("rev", 0) + 1
    board["savedAt"] = _iso_now()
    board["savedBy"] = "claude"
    p.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n")
    print(f"active board now {len(keep)} cards · rev → {board['rev']}")


if __name__ == "__main__":
    main()
