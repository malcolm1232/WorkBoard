"""Freeze ~/.claude/projects/<proj>/*.jsonl transcripts into corpora/{tiny,medium,large}.

Output structure:
  corpora/<size>/
    manifest.json    metadata (window, file count, total bytes, sha256 of manifest)
    transcripts/     copies of the JSONL files (deterministic-ordered)

Once written, the fixtures are byte-frozen. Re-running this script is idempotent
provided ~/.claude/projects/<proj>/ hasn't been pruned.

Inactivity window 2026-06-11 → 2026-06-15 is excluded from every fixture (see
memory: project_inactivity_gap_jun11_to_jun15). `large` therefore ends 2026-06-10
even if more days exist in theory.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import pathlib
import shutil
import sys

PROJECT_DIR = pathlib.Path("/Users/malco/.claude/projects/-Users-malco")
BENCH_DIR = pathlib.Path(__file__).parent
CORPORA_DIR = BENCH_DIR / "corpora"

# (size, start_iso_date, end_iso_date_inclusive) — windows chosen to avoid the
# 2026-06-11..15 inactivity gap.
FIXTURES = [
    ("tiny",   "2026-06-16", "2026-06-17"),
    ("medium", "2026-05-28", "2026-06-10"),
    ("large",  "2026-05-17", "2026-06-10"),
]


def first_timestamp_day(path: pathlib.Path) -> str | None:
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = d.get("timestamp")
                if ts and len(ts) >= 10:
                    return ts[:10]
    except Exception:
        return None
    return None


def build_fixture(size: str, start: str, end: str, force: bool = False) -> dict:
    out_dir = CORPORA_DIR / size
    transcripts_dir = out_dir / "transcripts"

    if out_dir.exists() and not force:
        manifest = json.loads((out_dir / "manifest.json").read_text())
        return {"size": size, "skipped": True, **manifest}

    if out_dir.exists():
        shutil.rmtree(out_dir)
    transcripts_dir.mkdir(parents=True)

    selected = []
    skipped_gap = 0
    for f in sorted(PROJECT_DIR.glob("*.jsonl")):
        day = first_timestamp_day(f)
        if not day:
            continue
        if "2026-06-11" <= day <= "2026-06-15":
            skipped_gap += 1
            continue
        if start <= day <= end:
            selected.append((day, f))

    selected.sort()  # (day, path) — deterministic ordering by day then UUID
    total_bytes = 0
    file_records = []
    for day, src in selected:
        dst = transcripts_dir / src.name
        shutil.copy2(src, dst)
        size_b = dst.stat().st_size
        total_bytes += size_b
        file_records.append({"day": day, "name": src.name, "bytes": size_b})

    # sha256 of the concatenated (day,name,bytes) records — content-addresses the fixture
    h = hashlib.sha256()
    for r in file_records:
        h.update(f"{r['day']}|{r['name']}|{r['bytes']}\n".encode())
    fingerprint = h.hexdigest()[:16]

    manifest = {
        "size": size,
        "window_start": start,
        "window_end": end,
        "files": len(file_records),
        "bytes": total_bytes,
        "fingerprint": fingerprint,
        "excluded_gap_files": skipped_gap,
        "inactivity_window": ["2026-06-11", "2026-06-15"],
        "source_dir": str(PROJECT_DIR),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild even if corpora/<size> exists")
    ap.add_argument("--size", choices=[f[0] for f in FIXTURES], help="build just one fixture")
    args = ap.parse_args()

    CORPORA_DIR.mkdir(exist_ok=True)
    targets = FIXTURES if not args.size else [f for f in FIXTURES if f[0] == args.size]

    for size, start, end in targets:
        m = build_fixture(size, start, end, force=args.force)
        if m.get("skipped"):
            print(f"  {size:6s} SKIP (exists)  files={m['files']}  bytes={m['bytes']}  fp={m['fingerprint']}")
        else:
            print(f"  {size:6s} BUILT          files={m['files']}  bytes={m['bytes']}  fp={m['fingerprint']}  ({start} → {end})")


if __name__ == "__main__":
    main()
