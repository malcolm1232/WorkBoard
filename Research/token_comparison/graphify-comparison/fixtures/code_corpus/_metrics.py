"""Board velocity metrics (Phase 5.5b · #114 BOARD-METRICS).

compute(d, since_days=7) → a plain dict answering "am I shipping faster?" and
"what's blocking me?" from the data already in board.json (createdAt / doneAt /
history). stdlib-only, no I/O, no side effects.

Served by serve.py GET /metrics?since=Nd and printed by card.py metrics.
"""
from __future__ import annotations

import datetime
import statistics


def _parse(iso: str | None) -> datetime.datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None
    # Normalise naive timestamps (older imports lack a Z/offset) to UTC so they
    # compare cleanly against now() and each other.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _entered_inprogress(card: dict) -> datetime.datetime | None:
    """When the card most recently entered In Progress. Prefer the history
    timeline (#258); fall back to updatedAt, then createdAt."""
    for h in reversed(card.get("history") or []):
        if h.get("to") == "inprogress":
            t = _parse(h.get("at"))
            if t:
                return t
    return _parse(card.get("updatedAt")) or _parse(card.get("createdAt"))


def compute(d: dict, since_days: int = 7) -> dict:
    now = _now()
    cutoff = now - datetime.timedelta(days=since_days)
    cards = d.get("cards", [])

    # ---- throughput: cards moved to Done per day in the window ----
    done_in_window = []
    per_day: dict[str, int] = {}
    for c in cards:
        if c.get("column") != "done":
            continue
        dt = _parse(c.get("doneAt"))
        if dt and dt >= cutoff:
            done_in_window.append(c)
            day = dt.date().isoformat()
            per_day[day] = per_day.get(day, 0) + 1

    # Dense series (zero-filled) for the last `since_days` days, oldest→newest.
    series = []
    for i in range(since_days - 1, -1, -1):
        day = (now - datetime.timedelta(days=i)).date().isoformat()
        series.append({"date": day, "count": per_day.get(day, 0)})

    # ---- cycle time: createdAt → doneAt for cards shipped in the window ----
    cycle_hours = []
    for c in done_in_window:
        created, done = _parse(c.get("createdAt")), _parse(c.get("doneAt"))
        if created and done and done >= created:
            cycle_hours.append((done - created).total_seconds() / 3600.0)

    # ---- per-column dwell time from history (#258) ----
    dwell: dict[str, list] = {}
    for c in cards:
        hist = c.get("history") or []
        for a, b in zip(hist, hist[1:]):
            ta, tb = _parse(a.get("at")), _parse(b.get("at"))
            if ta and tb and tb >= ta and a.get("to"):
                dwell.setdefault(a["to"], []).append((tb - ta).total_seconds() / 3600.0)
    dwell_median = {col: round(statistics.median(v), 1) for col, v in dwell.items() if v}

    # ---- blockers: In Progress and not touched in >7 days ----
    blockers = []
    for c in cards:
        if c.get("column") != "inprogress":
            continue
        entered = _entered_inprogress(c)
        if not entered:
            continue
        stuck_days = (now - entered).total_seconds() / 86400.0
        if stuck_days >= 7:
            blockers.append({
                "num": c.get("num"), "code": c.get("code") or "",
                "title": c.get("title", ""), "stuckDays": round(stuck_days, 1),
            })
    blockers.sort(key=lambda b: b["stuckDays"], reverse=True)

    # ---- priority drift: high-priority work that isn't moving ----
    open_cols = {"super-urgent", "ideas", "task", "backlog", "inprogress", "blocked"}
    not_started = {"super-urgent", "ideas", "task", "backlog"}
    open_cards = [c for c in cards if c.get("column") in open_cols]
    open_critical = sum(1 for c in open_cards if (c.get("priority") or "low") == "critical")
    open_mid = sum(1 for c in open_cards if (c.get("priority") or "low") == "mid")
    critical_not_started = sum(
        1 for c in cards
        if (c.get("priority") or "low") == "critical" and c.get("column") in not_started)

    wip = sum(1 for c in cards if c.get("column") == "inprogress")

    return {
        "sinceDays": since_days,
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rev": d.get("rev", 0),
        "totalCards": len(cards),
        "throughput": {
            "total": len(done_in_window),
            "perDay": series,
            "perDayAvg": round(len(done_in_window) / since_days, 2) if since_days else 0,
        },
        "cycleTime": {
            "count": len(cycle_hours),
            "medianHours": round(statistics.median(cycle_hours), 1) if cycle_hours else None,
            "medianDays": round(statistics.median(cycle_hours) / 24, 2) if cycle_hours else None,
        },
        "dwellMedianHours": dwell_median,
        "blockers": blockers,
        "wip": wip,
        "priority": {
            "openCritical": open_critical,
            "openMid": open_mid,
            "criticalNotStarted": critical_not_started,
        },
    }


def to_text(m: dict) -> str:
    """Compact human summary for `card.py metrics`."""
    tp, ct, pr = m["throughput"], m["cycleTime"], m["priority"]
    spark = _sparkline([d["count"] for d in tp["perDay"]])
    lines = [
        f"Velocity · last {m['sinceDays']}d · rev {m['rev']} · {m['totalCards']} cards",
        f"  throughput : {tp['total']} shipped  ({tp['perDayAvg']}/day)  {spark}",
        f"  cycle time : median {ct['medianDays']}d ({ct['count']} cards)" if ct["medianHours"] is not None
        else "  cycle time : (no completed cards in window)",
        f"  WIP        : {m['wip']} in progress",
        f"  priority   : {pr['openCritical']} open critical · {pr['criticalNotStarted']} critical not started · {pr['openMid']} open mid",
    ]
    if m["dwellMedianHours"]:
        dwell = " · ".join(f"{k} {v}h" for k, v in sorted(m["dwellMedianHours"].items()))
        lines.append(f"  dwell      : {dwell}")
    if m["blockers"]:
        lines.append(f"  blockers   : {len(m['blockers'])} stuck >7d in progress")
        for b in m["blockers"][:5]:
            lines.append(f"               #{b['num']} {b['code']} — {b['stuckDays']}d  {b['title'][:48]}")
    else:
        lines.append("  blockers   : none stuck >7d 🎉")
    return "\n".join(lines)


_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(vals: list[int]) -> str:
    if not vals:
        return ""
    hi = max(vals)
    if hi == 0:
        return _SPARK[0] * len(vals)
    return "".join(_SPARK[min(len(_SPARK) - 1, int(v / hi * (len(_SPARK) - 1)))] for v in vals)
