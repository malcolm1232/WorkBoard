"""Shared board renderers (Phase 5.5c · #115 BOARD-EXPORT).

One source of truth for turning a board dict into shareable artifacts:
  - to_markdown(d)  → narrative Markdown ("This sprint: shipped N items")
  - to_html(d)      → static, inline-styled HTML (no JS, printable)

Used by:
  - card.py wiki / card.py export   (CLI)
  - serve.py  GET /export.md / /export.html   (HTTP)

stdlib-only, no side effects, no I/O. Pass in a parsed board.json dict.
"""
from __future__ import annotations

import datetime
import html as _html

# Canonical left-to-right column order for grouping (mirrors card.py).
_ORDER = ["super-urgent", "ideas", "task",
          "backlog", "inprogress", "blocked", "done"]


def _now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        when = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = datetime.datetime.now(datetime.timezone.utc) - when
        hrs = int(delta.total_seconds() // 3600)
        if hrs < 1:
            return "<1h ago"
        if hrs < 24:
            return f"{hrs}h ago"
        return f"{hrs // 24}d ago"
    except Exception:
        return iso[:10]


def _within(iso: str | None, since_days: int | None) -> bool:
    """True if iso is within the last `since_days` (or since_days is None)."""
    if since_days is None:
        return True
    if not iso:
        return False
    try:
        when = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=since_days))
        return when >= cutoff
    except Exception:
        return False


def _col_names(d: dict) -> dict:
    return {c["id"]: c.get("name", c["id"]) for c in d.get("columns", [])}


def _grouped(d: dict) -> dict:
    by_col: dict[str, list] = {}
    for c in d.get("cards", []):
        by_col.setdefault(c.get("column", "?"), []).append(c)
    return by_col


def _ordered_cols(by_col: dict) -> list[str]:
    return [k for k in _ORDER if k in by_col] + [
        k for k in by_col if k not in _ORDER]


def _recent_done(d: dict, recent: int, since_days: int | None) -> list[dict]:
    done = [c for c in d.get("cards", [])
            if c.get("column") == "done" and _within(c.get("doneAt"), since_days)]
    done.sort(key=lambda c: c.get("doneAt") or "", reverse=True)
    return done[:recent] if recent else done


# ---------------------------------------------------------------- Markdown

def to_markdown(d: dict, recent: int = 10, since_days: int | None = None) -> str:
    cols = _col_names(d)
    by_col = _grouped(d)
    out = [f"# Board — rev {d.get('rev', 0)} · {len(d.get('cards', []))} cards",
           f"_generated {_now_iso()}_", ""]

    shipped = _recent_done(d, recent, since_days)
    if shipped:
        span = f"last {since_days}d" if since_days else f"last {len(shipped)}"
        out.append(f"## ✅ Recently shipped ({span})")
        for c in shipped:
            code = c.get("code") or ""
            out.append(f"- **#{c['num']} {code}** — {c.get('title','')}  ·  _{_ago(c.get('doneAt'))}_")
        out.append("")

    for col in [k for k in _ordered_cols(by_col) if k != "done"]:
        items = sorted(by_col[col], key=lambda c: c.get("updatedAt") or "", reverse=True)
        out.append(f"## {cols.get(col, col)} ({len(items)})")
        for c in items:
            p = (c.get("priority") or "-")[:1].upper()
            subs = c.get("subtasks") or []
            prog = f" · {sum(1 for s in subs if s.get('done'))}/{len(subs)}" if subs else ""
            out.append(f"- `[{p}]` **#{c['num']} {c.get('code') or ''}** — {c.get('title','')}{prog}")
        out.append("")

    return "\n".join(out)


# -------------------------------------------------------------------- HTML

_CSS = """
:root{--bg:#0f1115;--panel:#171a21;--ink:#e6e9ef;--mute:#8b93a3;--line:#262b35;
--c:#C84B4B;--m:#D9A441;--l:#5a7d9a;--ship:#3D8F65}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:32px}
h1{font-size:24px;margin:0 0 4px}
.meta{color:var(--mute);font-size:13px;margin-bottom:28px}
h2{font-size:16px;margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.ship h2{color:var(--ship)}
ul{list-style:none;padding:0;margin:0}
li{padding:7px 0;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:baseline}
.num{color:var(--mute);font-variant-numeric:tabular-nums;min-width:46px}
.code{font:600 12px ui-monospace,Menlo,monospace;color:var(--mute)}
.prio{font:600 10px ui-monospace,monospace;padding:1px 6px;border-radius:4px;background:var(--line)}
.prio.C{background:var(--c);color:#fff}.prio.M{background:var(--m);color:#221b00}.prio.L{background:var(--l);color:#fff}
.title{flex:1}
.prog{color:var(--mute);font-size:12px;font-variant-numeric:tabular-nums}
.ago{color:var(--mute);font-size:12px}
footer{margin-top:40px;color:var(--mute);font-size:12px}
@media print{body{background:#fff;color:#111}.prio{border:1px solid #999}}
"""


def _esc(s: str) -> str:
    return _html.escape(str(s or ""))


def to_html(d: dict, recent: int = 10, since_days: int | None = None) -> str:
    cols = _col_names(d)
    by_col = _grouped(d)
    rev, n = d.get("rev", 0), len(d.get("cards", []))

    parts = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>Board snapshot — rev {rev}</title><style>{_CSS}</style></head><body>",
             f"<h1>Board snapshot</h1>",
             f"<div class='meta'>rev {rev} · {n} cards · generated {_esc(_now_iso())}</div>"]

    shipped = _recent_done(d, recent, since_days)
    if shipped:
        span = f"last {since_days}d" if since_days else f"last {len(shipped)}"
        parts.append(f"<section class='ship'><h2>✅ Recently shipped ({_esc(span)})</h2><ul>")
        for c in shipped:
            parts.append(
                f"<li><span class='num'>#{c['num']}</span>"
                f"<span class='code'>{_esc(c.get('code'))}</span>"
                f"<span class='title'>{_esc(c.get('title'))}</span>"
                f"<span class='ago'>{_esc(_ago(c.get('doneAt')))}</span></li>")
        parts.append("</ul></section>")

    for col in [k for k in _ordered_cols(by_col) if k != "done"]:
        items = sorted(by_col[col], key=lambda c: c.get("updatedAt") or "", reverse=True)
        parts.append(f"<section><h2>{_esc(cols.get(col, col))} ({len(items)})</h2><ul>")
        for c in items:
            p = (c.get("priority") or "-")[:1].upper()
            subs = c.get("subtasks") or []
            prog = (f"<span class='prog'>{sum(1 for s in subs if s.get('done'))}/{len(subs)}</span>"
                    if subs else "")
            parts.append(
                f"<li><span class='prio {p if p in 'CML' else ''}'>{p}</span>"
                f"<span class='num'>#{c['num']}</span>"
                f"<span class='code'>{_esc(c.get('code'))}</span>"
                f"<span class='title'>{_esc(c.get('title'))}</span>{prog}</li>")
        parts.append("</ul></section>")

    parts.append("<footer>board-steward snapshot · static HTML, no JS</footer>")
    parts.append("</body></html>")
    return "".join(parts)
