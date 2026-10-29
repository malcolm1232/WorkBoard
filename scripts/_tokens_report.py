"""Token + API-equivalent cost reports over the #958 index (_tokens.py).

compute(con, since_days, project=None) returns a plain dict; to_text() renders
it for `card.py tokens`; serve.py GET /tokens returns the dict as JSON. Mirrors
_metrics.py: pure reads, no writes.

Every dollar figure is API-equivalent list price, not a bill. Every
attribution carries its confidence: exact (working folder, the session's
last-active board, the parent session, the card.py event log), inferred
(touched-path vote, card.py calls backfilled from transcripts) or
unattributed. Output tokens of old partial-usage replies are ESTIMATED and
reported as such.
"""
from __future__ import annotations

import datetime
import json
import os
import statistics
from pathlib import Path

import _tokens

PROJECT_CONFIDENCE = {
    "cwd": "exact", "session-cwd": "exact", "last-active": "exact", "parent": "exact",
    "paths-board": "inferred", "paths-folder": "inferred", "none": "unattributed",
}
CARD_CONFIDENCE = {"log": "exact", "backfill": "inferred", None: "unattributed"}
MAIN_KINDS = ("main-interactive", "main-headless")
AGENT_KINDS = ("sub-agent", "workflow-agent", "headless-child")
TOP = 12


def _r(x, nd=2):
    return round(x or 0.0, nd)


def _window(since_days: int):
    today = datetime.date.today()
    start = today - datetime.timedelta(days=since_days - 1)
    return start.isoformat(), today.isoformat()


_LABELS: dict = {}


def project_label(key: str | None, folders: _tokens.FolderKeys | None = None) -> str:
    """One naming rule for every table: an alias from folders.json, else a
    board project's switcher name (its board title, cleaned like the header
    tabs), else the folder name."""
    if not key:
        return "(unattributed)"
    if folders and key in folders.aliases:
        return folders.aliases[key]
    if key not in _LABELS:
        name = None
        try:
            d = json.loads((Path(key) / "board" / "board.json").read_text())
            name = _tokens.clean_board_title(d.get("title")) or None
        except Exception:
            name = None
        _LABELS[key] = name or os.path.basename(key.rstrip("/")) or key
    return _LABELS[key]


def _totals(rows) -> dict:
    t = dict(n=0, n_est=0, n_unpriced=0, n_error=0, inp=0, out=0, out_raw=0, think=0,
             out_think_known=0, cr=0, w5=0, w1=0, ws=0, wf=0, cost=0.0, cost_raw=0.0,
             cost_est=0.0, c_inp=0.0, c_out=0.0, c_cr=0.0, c_w5=0.0, c_w1=0.0, c_web=0.0,
             break_excess=0.0)
    for r in rows:
        for k in t:
            t[k] += r[k] or 0
    return t


def _shape_totals(t: dict) -> dict:
    prompt = t["inp"] + t["cr"] + t["w5"] + t["w1"]
    visible_known = t["out_think_known"] - t["think"]
    return {
        "replies": t["n"], "cost": _r(t["cost"]), "costRaw": _r(t["cost_raw"]),
        "estimated": {"replies": t["n_est"], "cost": _r(t["cost_est"]),
                      "share": _r(t["cost_est"] / t["cost"], 4) if t["cost"] else 0,
                      "outputLift": _r(t["cost"] - t["cost_raw"])},
        "unpricedReplies": t["n_unpriced"], "errorReplies": t["n_error"],
        "components": {"uncachedInput": _r(t["c_inp"]), "output": _r(t["c_out"]),
                       "cacheRead": _r(t["c_cr"]), "cacheWrite5m": _r(t["c_w5"]),
                       "cacheWrite1h": _r(t["c_w1"]), "webSearch": _r(t["c_web"])},
        "tokens": {"uncachedInput": t["inp"], "cacheRead": t["cr"], "cacheWrite5m": t["w5"],
                   "cacheWrite1h": t["w1"], "fresh": t["inp"] + t["w5"] + t["w1"],
                   "output": t["out"], "outputRecorded": t["out_raw"],
                   "outputThinking": t["think"], "outputVisible": max(0, visible_known),
                   "outputSplitUnknown": t["out"] - t["out_think_known"],
                   "webSearches": t["ws"], "webFetches": t["wf"]},
        "cacheHitRate": _r(t["cr"] / prompt, 4) if prompt else 0,
        "cacheBreakExcess": _r(t["break_excess"]),
    }


def _group(rows, keyf) -> dict:
    g: dict = {}
    for r in rows:
        g.setdefault(keyf(r), []).append(r)
    return g


def _pct(xs, q):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def compute(con, since_days: int = 7, project: str | None = None) -> dict:
    start, end = _window(since_days)
    folders = _tokens.FolderKeys()
    titles = _tokens.BoardCards()
    _LABELS.clear()
    pf = " AND project = ?" if project else ""
    pa = [project] if project else []
    daily = [dict(r) for r in con.execute(
        f"SELECT * FROM daily WHERE day >= ? AND day <= ?{pf}", [start, end] + pa)]
    tot = _totals(daily)
    out: dict = {
        "sinceDays": since_days, "from": start, "to": end,
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": project, "projectLabel": project_label(project, folders) if project else None,
        "pricesAsOf": _tokens.PRICES_AS_OF, "basis": "API-equivalent list price, not a bill",
        "lastScan": _last_scan(con),
        "totals": _shape_totals(tot),
    }
    cost = tot["cost"] or 0.0

    # ---- confidence of attribution ----
    conf_p: dict = {"exact": 0.0, "inferred": 0.0, "unattributed": 0.0}
    conf_c: dict = {"exact": 0.0, "inferred": 0.0, "unattributed": 0.0}
    rules: dict = {}
    for r in daily:
        conf_p[PROJECT_CONFIDENCE.get(r["project_rule"], "unattributed")] += r["cost"] or 0
        conf_c[CARD_CONFIDENCE.get(r["card_rule"], "unattributed")] += r["cost"] or 0
        rules[r["project_rule"] or "none"] = rules.get(r["project_rule"] or "none", 0) + (r["cost"] or 0)
    out["confidence"] = {
        "project": {k: {"cost": _r(v), "share": _r(v / cost, 4) if cost else 0}
                    for k, v in conf_p.items()},
        "card": {k: {"cost": _r(v), "share": _r(v / cost, 4) if cost else 0}
                 for k, v in conf_c.items()},
        "projectRules": {k: _r(v) for k, v in sorted(rules.items(), key=lambda kv: -kv[1])},
    }

    def rollup(keyf, extra=None, limit=None):
        res = []
        for k, rs in _group(daily, keyf).items():
            t = _totals(rs)
            row = {"key": k, "cost": _r(t["cost"]), "replies": t["n"],
                   "share": _r(t["cost"] / cost, 4) if cost else 0,
                   "estimatedCost": _r(t["cost_est"]),
                   "cacheHitRate": _r(t["cr"] / max(1, t["inp"] + t["cr"] + t["w5"] + t["w1"]), 4)}
            if extra:
                row.update(extra(k, rs, t))
            res.append(row)
        res.sort(key=lambda x: -x["cost"])
        return res[:limit] if limit else res

    def conf_split(rs, field, table):
        c = {"exact": 0.0, "inferred": 0.0, "unattributed": 0.0}
        for r in rs:
            c[table.get(r[field], "unattributed")] += r["cost"] or 0
        return {k: _r(v) for k, v in c.items()}

    out["byModel"] = rollup(lambda r: r["model"] or "?",
                            lambda k, rs, t: {"priced": t["n_unpriced"] == 0,
                                              "perReply": _r(t["cost"] / t["n"], 4) if t["n"] else 0})
    out["byKind"] = rollup(lambda r: r["kind"] or "?")
    agents = sum(x["cost"] for x in out["byKind"] if x["key"] in AGENT_KINDS)
    out["mainVsAgents"] = {"main": _r(cost - agents), "agents": _r(agents),
                           "agentShare": _r(agents / cost, 4) if cost else 0}
    days = {x["key"]: x for x in rollup(lambda r: r["day"])}
    dd = []
    d0 = datetime.date.fromisoformat(start)
    for i in range(since_days):
        k = (d0 + datetime.timedelta(days=i)).isoformat()
        x = days.get(k)
        dd.append({"day": k, "cost": x["cost"] if x else 0.0, "replies": x["replies"] if x else 0})
    out["byDay"] = dd

    def proj_extra(k, rs, t):
        rule = max(_group(rs, lambda r: r["project_rule"]).items(),
                   key=lambda kv: sum(x["cost"] or 0 for x in kv[1]))[0]
        return {"label": project_label(k, folders),
                "folder": rule == "paths-folder",
                "isBoard": bool(k) and os.path.exists(os.path.join(k, "board", "board.json")),
                "confidence": conf_split(rs, "project_rule", PROJECT_CONFIDENCE)}
    out["byProject"] = rollup(lambda r: r["project"], proj_extra)

    def card_extra(k, rs, t):
        conf = conf_split(rs, "card_rule", CARD_CONFIDENCE)
        if not k:
            return {"label": "(no card)", "num": None, "board": None, "project": None,
                    "title": None, "confidence": conf}
        board, _, num = k.rpartition("#")
        proj = project_label(os.path.dirname(board), folders)
        return {"label": f"{proj} #{num}", "num": num, "board": board, "project": proj,
                "title": titles.title(board, num), "confidence": conf}
    out["byCard"] = rollup(lambda r: r["card"], card_extra)

    out.update(_turns(con, start, end, project))
    out["cacheBreaks"] = _breaks(con, start, end, project)
    out["contextRent"] = _rent(con, start, end, project)
    out["sessions"] = _sessions(con, start, end, project, folders)
    out["baseContext"] = _base_context(con, start, end, project)
    out["compactions"] = _compactions(con, start, end, project)
    out["insights"] = _insights(out)
    return out


def _last_scan(con):
    r = con.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
    if not r:
        return None
    return datetime.datetime.fromtimestamp(float(r[0]), datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _turns(con, start, end, project):
    """Turn classes (chat vs action kinds), triggers and the top turns. A turn's
    cost = its own replies + the agents it spawned (Agent by meta.toolUseId,
    Workflow by toolUseResult.runId)."""
    pf = " AND f.project = ?" if project else ""
    pa = [project] if project else []
    turns = {}
    for r in con.execute(
            "SELECT t.turn_id, t.trigger, t.day, t.start_ts, f.reply_kind FROM turns t "
            f"JOIN files f ON f.id = t.file_id WHERE t.day >= ? AND t.day <= ?{pf}",
            [start, end] + pa):
        turns[r[0]] = {"trigger": r[1], "day": r[2], "start": r[3], "kind": r[4],
                       "own": 0.0, "child": 0.0, "replies": 0, "tools": set()}
    if not turns:
        return {"turnClasses": [], "turnTriggers": [], "topTurns": [],
                "chatVsAction": {"chat": {"turns": 0, "cost": 0}, "action": {"turns": 0, "cost": 0}}}
    for r in con.execute("SELECT turn_id, cost, tools FROM replies WHERE day >= ? AND day <= ? "
                         "AND turn_id IS NOT NULL", (start, end)):
        t = turns.get(r[0])
        if t is None:
            continue
        t["own"] += r[1] or 0
        t["replies"] += 1
        if r[2]:
            t["tools"].update(r[2].split(","))
    # children -> spawning turn
    child_turn = {}
    for r in con.execute(
            "SELECT f.id, s.turn_id FROM files f JOIN spawns s ON s.tool_use_id = f.spawn_tool_use "
            "WHERE f.kind = 'sub-agent'"):
        child_turn[r[0]] = r[1]
    for r in con.execute(
            "SELECT f.id, s.turn_id FROM files f JOIN wf_runs w ON w.run_id = f.run_id "
            "JOIN spawns s ON s.tool_use_id = w.tool_use_id WHERE f.kind = 'workflow-agent'"):
        child_turn[r[0]] = r[1]
    for r in con.execute("SELECT file_id, SUM(cost) FROM replies WHERE day >= ? AND day <= ? "
                         "AND kind IN ('sub-agent', 'workflow-agent') GROUP BY file_id",
                         (start, end)):
        t = turns.get(child_turn.get(r[0]))
        if t is not None:
            t["child"] += r[1] or 0
    classes: dict = {}
    trig: dict = {}
    for tid, t in turns.items():
        if not t["replies"] and not t["child"]:
            continue
        c = _tokens.turn_class(t["tools"])
        t["class"] = c
        g = classes.setdefault(c, {"turns": 0, "own": 0.0, "children": 0.0, "replies": 0,
                                   "totals": []})
        g["turns"] += 1
        g["own"] += t["own"]
        g["children"] += t["child"]
        g["replies"] += t["replies"]
        g["totals"].append(t["own"] + t["child"])
        h = trig.setdefault(t["trigger"], {"turns": 0, "cost": 0.0})
        h["turns"] += 1
        h["cost"] += t["own"] + t["child"]
    cls = []
    for c, g in classes.items():
        cls.append({"key": c, "label": _tokens.TURN_CLASS_LABEL.get(c, c), "turns": g["turns"],
                    "own": _r(g["own"]), "children": _r(g["children"]),
                    "total": _r(g["own"] + g["children"]),
                    "repliesPerTurn": _r(g["replies"] / g["turns"], 1),
                    "median": _r(statistics.median(g["totals"])), "p90": _r(_pct(g["totals"], 0.9))})
    cls.sort(key=lambda x: -x["total"])
    chat = classes.get("chat", {"turns": 0, "own": 0.0, "children": 0.0})
    act_turns = sum(g["turns"] for c, g in classes.items() if c != "chat")
    act_cost = sum(g["own"] + g["children"] for c, g in classes.items() if c != "chat")
    top = sorted((t for t in turns.values() if "class" in t),
                 key=lambda t: -(t["own"] + t["child"]))[:TOP]
    return {
        "turnClasses": cls,
        "chatVsAction": {"chat": {"turns": chat["turns"], "cost": _r(chat["own"] + chat["children"])},
                         "action": {"turns": act_turns, "cost": _r(act_cost)}},
        "turnTriggers": sorted(({"key": k, "turns": v["turns"], "cost": _r(v["cost"])}
                                for k, v in trig.items()), key=lambda x: -x["cost"]),
        "topTurns": [{"start": t["start"], "trigger": t["trigger"], "class": t["class"],
                      "replies": t["replies"], "own": _r(t["own"]), "children": _r(t["child"])}
                     for t in top],
    }


def _breaks(con, start, end, project):
    pf = " AND project = ?" if project else ""
    pa = [project] if project else []
    res = []
    for r in con.execute(
            "SELECT CASE WHEN kind IN ('main-interactive', 'main-headless') THEN 'main' ELSE 'agents' END, "
            "break_cause, COUNT(*), SUM(w5 + w1), SUM(break_excess) FROM replies "
            f"WHERE day >= ? AND day <= ? AND break_cause IS NOT NULL{pf} GROUP BY 1, 2",
            [start, end] + pa):
        res.append({"who": r[0], "cause": r[1], "n": r[2], "tokensWritten": r[3] or 0,
                    "excess": _r(r[4])})
    res.sort(key=lambda x: -x["excess"])
    writes = con.execute(f"SELECT SUM(c_w5 + c_w1) FROM replies WHERE day >= ? AND day <= ?{pf}",
                         [start, end] + pa).fetchone()[0] or 0.0
    return {"total": _r(sum(x["excess"] for x in res)), "allWrites": _r(writes), "byCause": res}


def _rent(con, start, end, project):
    """Context rent ("bloat"): what a tool result or an injected context block
    costs to write once and re-read in every later reply until compaction."""
    pf = " AND f.project = ?" if project else ""
    pa = [project] if project else []
    res = {"tool": [], "inject": []}
    for r in con.execute(
            "SELECT r.kind, r.key, SUM(r.n), SUM(r.tokens), SUM(r.write_cost), SUM(r.carry_cost) "
            "FROM rent r JOIN files f ON f.id = r.file_id "
            f"WHERE r.day >= ? AND r.day <= ?{pf} GROUP BY 1, 2", [start, end] + pa):
        res.setdefault(r[0], []).append({"key": r[1], "n": r[2] or 0, "tokens": int(r[3] or 0),
                                         "write": _r(r[4]), "carry": _r(r[5]),
                                         "total": _r((r[4] or 0) + (r[5] or 0))})
    out = {}
    for k, v in res.items():
        v.sort(key=lambda x: -x["total"])
        out[k] = {"total": _r(sum(x["total"] for x in v)), "top": v[:TOP]}
    hooks = [x for x in res.get("inject", []) if x["key"].split(":", 1)[-1].startswith("hook_")
             and x["key"].endswith(".sh")]
    out["boardHooks"] = {"n": sum(x["n"] for x in hooks), "total": _r(sum(x["total"] for x in hooks))}
    return out


def _sessions(con, start, end, project, folders):
    """Root sessions (a delegator run = the root plus every descendant)."""
    pf = " AND r.project = ?" if project else ""
    pa = [project] if project else []
    g: dict = {}
    for r in con.execute(
            "SELECT f.root_session, f.reply_kind, f.session_id, r.model, SUM(r.cost), COUNT(*), "
            "MIN(r.ts), MAX(r.ts), r.project FROM replies r JOIN files f ON f.id = r.file_id "
            f"WHERE r.day >= ? AND r.day <= ?{pf} GROUP BY 1, 2, 3, 4, 9", [start, end] + pa):
        s = g.setdefault(r[0], {"session": r[0], "own": 0.0, "children": 0.0, "replies": 0,
                                "models": {}, "first": r[6], "last": r[7], "projects": {},
                                "agents": set()})
        if r[2] == r[0] and r[1] in MAIN_KINDS:
            s["own"] += r[4] or 0
            s["models"][r[3]] = s["models"].get(r[3], 0) + (r[4] or 0)
        else:
            s["children"] += r[4] or 0
        s["replies"] += r[5]
        s["first"] = min(s["first"] or r[6], r[6] or s["first"])
        s["last"] = max(s["last"] or r[7], r[7] or s["last"])
        s["projects"][r[8]] = s["projects"].get(r[8], 0) + (r[4] or 0)
    for r in con.execute("SELECT root_session, COUNT(*) FROM files WHERE kind != 'main' "
                         "GROUP BY 1"):
        if r[0] in g:
            g[r[0]]["agentFiles"] = r[1]
    rows = []
    for s in g.values():
        total = s["own"] + s["children"]
        proj = max(s["projects"].items(), key=lambda kv: kv[1])[0] if s["projects"] else None
        rows.append({"session": s["session"], "own": _r(s["own"]), "children": _r(s["children"]),
                     "total": _r(total), "replies": s["replies"], "agents": s.get("agentFiles", 0),
                     "plannerModel": max(s["models"].items(), key=lambda kv: kv[1])[0]
                     if s["models"] else None,
                     "project": proj, "projectLabel": project_label(proj, folders),
                     "first": s["first"], "last": s["last"]})
    rows.sort(key=lambda x: -x["total"])
    total = sum(x["total"] for x in rows)
    top5 = sum(x["total"] for x in rows[:5])
    return {"count": len(rows), "top5Share": _r(top5 / total, 4) if total else 0,
            "top": rows[:TOP]}


def _base_context(con, start, end, project):
    pf = " AND r.project = ?" if project else ""
    pa = [project] if project else []
    by: dict = {}
    for r in con.execute(
            "SELECT r.kind, r.prompt FROM replies r JOIN (SELECT file_id, MIN(line) AS line FROM "
            "replies WHERE model != '<synthetic>' GROUP BY file_id) m ON m.file_id = r.file_id "
            f"AND m.line = r.line WHERE r.day >= ? AND r.day <= ?{pf}", [start, end] + pa):
        by.setdefault(r[0], []).append(r[1] or 0)
    return [{"kind": k, "files": len(v), "median": int(statistics.median(v)),
             "p90": int(_pct(v, 0.9))} for k, v in sorted(by.items())]


def _compactions(con, start, end, project):
    pf = " AND f.project = ?" if project else ""
    pa = [project] if project else []
    return [{"trigger": r[0] or "?", "n": r[1]} for r in con.execute(
        "SELECT c.trigger, COUNT(*) FROM compactions c JOIN files f ON f.id = c.file_id "
        f"WHERE c.day >= ? AND c.day <= ?{pf} GROUP BY 1", [start, end] + pa)]


def _insights(o: dict) -> list[str]:
    """The spec's computed headline findings (6.3), as short sentences."""
    t = o["totals"]
    cost = t["cost"]
    ins = []
    if not cost:
        return ins
    mva = o["mainVsAgents"]
    kinds = {x["key"]: x for x in o["byKind"]}
    sub = kinds.get("sub-agent", {}).get("share", 0)
    wf = kinds.get("workflow-agent", {}).get("share", 0)
    ins.append(f"Agents are {mva['agentShare']:.0%} of spend (sub-agents {sub:.0%}, "
               f"workflow agents {wf:.0%}).")
    s = o["sessions"]
    if s["count"] >= 5:
        ins.append(f"The 5 costliest session trees are {s['top5Share']:.0%} of spend "
                   f"across {s['count']} sessions.")
    deleg = next((c for c in o["turnClasses"] if c["key"] == "delegating"), None)
    if deleg:
        ins.append(f"A delegating turn costs a median ${deleg['median']:.2f} including its "
                   f"agents (p90 ${deleg['p90']:.2f}).")
    br = o["cacheBreaks"]
    if br["byCause"]:
        b0 = br["byCause"][0]
        ins.append(f"Cache breaks cost ${br['total']:,.0f} above cache hits; the biggest cause is "
                   f"{b0['cause']} ({b0['who']}, ${b0['excess']:,.0f}).")
    c = t["components"]
    parts = sorted(((c["cacheRead"], "Cache read is"), (c["output"], "Output is"),
                    (c["cacheWrite5m"] + c["cacheWrite1h"], "Cache writes are")), reverse=True)
    ins.append(f"{parts[0][1]} the largest component ({parts[0][0] / cost:.0%}); "
               f"uncached input is {c['uncachedInput'] / cost:.1%}; cache hit rate "
               f"{t['cacheHitRate']:.1%}.")
    tr = o["contextRent"].get("tool", {}).get("top") or []
    if tr:
        ins.append(f"{tr[0]['key']} output is the largest context rent "
                   f"(${tr[0]['total']:,.0f})" + (f", then {tr[1]['key']} (${tr[1]['total']:,.0f})."
                                                   if len(tr) > 1 else "."))
    if o["byModel"]:
        m = o["byModel"][0]
        ins.append(f"{m['key']} is {m['share']:.0%} of spend at ${m['perReply']:.3f} per reply.")
    chat = o["chatVsAction"]["chat"]
    if chat["turns"]:
        ins.append(f"Chat-only turns: {chat['turns']} turns, ${chat['cost']:,.0f} "
                   f"({chat['cost'] / cost:.1%} of spend).")
    bh = o["contextRent"].get("boardHooks") or {}
    if bh.get("n"):
        ins.append(f"WorkBoard's own hook injections cost ${bh['total']:,.2f} "
                   f"({bh['n']} injections).")
    est = t["estimated"]
    if est["replies"]:
        ins.append(f"{est['share']:.0%} of spend is on replies whose output is estimated "
                   f"(old partial-usage transcripts, +${est['outputLift']:,.0f} vs recorded).")
    unpriced = t["unpricedReplies"]
    if unpriced:
        ins.append(f"{unpriced} replies use an unpriced model and are counted in tokens only.")
    return ins


# ===== text =====

def _money(x):
    return f"${x:,.2f}" if abs(x) < 100 else f"${x:,.0f}"


def _tok(n):
    n = n or 0
    for unit, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= d:
            return f"{n / d:.1f}{unit}"
    return str(int(n))


def _conf(c):
    return " / ".join(f"{k} {_money(v)}" for k, v in c.items() if v)


BY_CHOICES = ("summary", "project", "session", "card", "model", "kind", "day", "tool",
              "turn-kind", "turn", "breaks")


def to_text(o: dict, by: str = "summary") -> str:
    t = o["totals"]
    head = (f"Tokens · last {o['sinceDays']}d ({o['from']} to {o['to']})"
            + (f" · project {o['projectLabel']}" if o.get("project") else "")
            + f" · API-equivalent $, prices as of {o['pricesAsOf']}")
    L = [head]
    est = t["estimated"]
    L.append(f"  total      : {_money(t['cost'])} over {t['replies']:,} replies"
             + (f"  (incl. {_money(est['cost'])} on {est['replies']:,} replies with ESTIMATED "
                f"output; recorded-only {_money(t['costRaw'])})" if est["replies"] else ""))
    if t["unpricedReplies"]:
        L.append(f"  unpriced   : {t['unpricedReplies']} replies on unknown models (tokens only)")
    if by == "summary":
        c, k = t["components"], t["tokens"]
        L.append(f"  cost parts : output {_money(c['output'])} · cache read {_money(c['cacheRead'])}"
                 f" · write 5m {_money(c['cacheWrite5m'])} · write 1h {_money(c['cacheWrite1h'])}"
                 f" · uncached input {_money(c['uncachedInput'])} · web {_money(c['webSearch'])}")
        L.append(f"  tokens     : uncached in {_tok(k['uncachedInput'])} · cache read "
                 f"{_tok(k['cacheRead'])} · write 5m {_tok(k['cacheWrite5m'])} · write 1h "
                 f"{_tok(k['cacheWrite1h'])} · hit rate {t['cacheHitRate']:.1%}")
        L.append(f"  output     : {_tok(k['output'])} = visible {_tok(k['outputVisible'])} + "
                 f"thinking {_tok(k['outputThinking'])} + split unknown {_tok(k['outputSplitUnknown'])}")
        m = o["mainVsAgents"]
        L.append(f"  main/agents: main {_money(m['main'])} · agents {_money(m['agents'])} "
                 f"({m['agentShare']:.0%})")
        cva = o["chatVsAction"]
        L.append(f"  turns      : chat {cva['chat']['turns']} turns {_money(cva['chat']['cost'])} · "
                 f"action {cva['action']['turns']} turns {_money(cva['action']['cost'])}")
        L.append(f"  cache break: {_money(o['cacheBreaks']['total'])} excess over hits")
        cp, cc = o["confidence"]["project"], o["confidence"]["card"]
        L.append("  project by : " + " · ".join(f"{k} {v['share']:.0%}" for k, v in cp.items()))
        L.append("  card by    : " + " · ".join(f"{k} {v['share']:.0%}" for k, v in cc.items()))
        L.append("  top projects:")
        for p in o["byProject"][:5]:
            L.append(f"    {p['label'][:28]:28s} {_money(p['cost']):>10s}  {p['share']:5.1%}  "
                     f"{_conf(p['confidence'])}")
        L.append("  top cards:")
        for p in [x for x in o["byCard"] if x["key"]][:5]:
            L.append(f"    {p['label'][:22]:22s} {_money(p['cost']):>10s}  {(p.get('title') or '')[:44]}")
        if o["insights"]:
            L.append("  insights:")
            L.extend(f"    - {s}" for s in o["insights"])
        return "\n".join(L)

    def table(rows, label, extra=lambda r: ""):
        for r in rows:
            L.append(f"  {str(label(r))[:40]:40s} {_money(r['cost']):>10s} {r.get('share', 0):6.1%}"
                     f"  {extra(r)}")

    if by == "project":
        table(o["byProject"], lambda r: r["label"] + (" (folder)" if r["folder"] else ""),
              lambda r: _conf(r["confidence"]))
    elif by == "card":
        table(o["byCard"], lambda r: f"{r['label']} {r.get('title') or ''}",
              lambda r: _conf(r["confidence"]))
    elif by == "model":
        table(o["byModel"], lambda r: r["key"] + ("" if r["priced"] else " (UNPRICED)"),
              lambda r: f"${r['perReply']:.3f}/reply")
    elif by == "kind":
        table(o["byKind"], lambda r: r["key"])
    elif by == "day":
        for d in o["byDay"]:
            L.append(f"  {d['day']}  {_money(d['cost']):>10s}  {d['replies']:>7,} replies")
    elif by == "session":
        for s in o["sessions"]["top"]:
            L.append(f"  {s['session'][:8]}  {_money(s['total']):>10s}  own {_money(s['own']):>9s}"
                     f"  agents {_money(s['children']):>9s} ({s['agents']} files)  "
                     f"{s['projectLabel'][:20]}  {s['plannerModel'] or '-'}")
    elif by == "turn-kind":
        L.append(f"  {'class':18s} {'turns':>6s} {'own':>10s} {'agents':>10s} {'median':>8s} {'p90':>8s}")
        for c in o["turnClasses"]:
            L.append(f"  {c['label'][:18]:18s} {c['turns']:>6,} {_money(c['own']):>10s} "
                     f"{_money(c['children']):>10s} {_money(c['median']):>8s} {_money(c['p90']):>8s}")
        L.append("  triggers: " + " · ".join(f"{x['key']} {x['turns']} {_money(x['cost'])}"
                                             for x in o["turnTriggers"]))
    elif by == "turn":
        for x in o["topTurns"]:
            L.append(f"  {x['start'][:16]}  {x['class']:10s} {x['trigger']:18s} {x['replies']:>4} "
                     f"replies  own {_money(x['own']):>8s}  agents {_money(x['children']):>9s}")
    elif by == "tool":
        r = o["contextRent"]
        L.append(f"  context rent of tool results: {_money(r.get('tool', {}).get('total', 0))}")
        for x in r.get("tool", {}).get("top", []):
            L.append(f"    {x['key'][:36]:36s} {x['n']:>7,} results {_tok(x['tokens']):>7s} tok "
                     f" {_money(x['total']):>9s}")
        L.append(f"  context rent of injected context: {_money(r.get('inject', {}).get('total', 0))}")
        for x in r.get("inject", {}).get("top", []):
            L.append(f"    {x['key'][:36]:36s} {x['n']:>7,} blocks  {_tok(x['tokens']):>7s} tok "
                     f" {_money(x['total']):>9s}")
    elif by == "breaks":
        b = o["cacheBreaks"]
        L.append(f"  cache breaks: {_money(b['total'])} excess of {_money(b['allWrites'])} writes")
        for x in b["byCause"]:
            L.append(f"    {x['who']:6s} {x['cause'][:36]:36s} {x['n']:>6,}  {_tok(x['tokensWritten']):>7s}"
                     f"  {_money(x['excess']):>9s}")
    return "\n".join(L)
