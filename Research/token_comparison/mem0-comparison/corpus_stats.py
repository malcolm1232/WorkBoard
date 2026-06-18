"""Compute fairness-neutral stats over a frozen fixture corpus.

Used by both adapters' ingest models so they measure the SAME corpus the same
way: number of distinct sessions (claude-mem compresses one per SessionEnd),
number of user/assistant turns, and total transcript tokens (the text either
system must read to build memory).
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))
import tokencount  # noqa: E402


def _msg_text(o: dict) -> str:
    m = o.get("message")
    if isinstance(m, dict):
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"
            )
    if isinstance(o.get("content"), str):
        return o["content"]
    return ""


def corpus_stats(corpus_dir: Path) -> dict:
    corpus_dir = Path(corpus_dir)
    tdir = corpus_dir / "transcripts"
    manifest = json.loads((corpus_dir / "manifest.json").read_text())

    sessions = set()
    sessions_with_work = set()
    turns = 0
    transcript_tokens = 0

    for jf in sorted(tdir.glob("*.jsonl")):
        sid = jf.stem
        had_work = False
        with jf.open(errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                sessions.add(o.get("sessionId") or sid)
                tp = o.get("type")
                if tp in ("user", "assistant"):
                    txt = _msg_text(o)
                    if txt:
                        turns += 1
                        transcript_tokens += tokencount.count(txt)
                        if tp == "user":
                            had_work = True
        if had_work:
            sessions_with_work.add(sid)

    return {
        "corpus": corpus_dir.name,
        "fingerprint": manifest.get("fingerprint"),
        "files": manifest.get("files"),
        "window": [manifest.get("window_start"), manifest.get("window_end")],
        "sessions": len(sessions),
        "sessions_with_work": len(sessions_with_work),
        "turns": turns,
        "transcript_tokens": transcript_tokens,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", type=Path)
    args = ap.parse_args()
    print(json.dumps(corpus_stats(args.corpus), indent=2))
