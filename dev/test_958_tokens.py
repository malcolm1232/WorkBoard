#!/usr/bin/env python3
"""#958: token index + API-equivalent cost - collector, pricing, attribution,
incremental re-index, card events, report.

Harness: synthetic transcripts in a throwaway projects dir (CLAUDE_PROJECTS_DIR),
a throwaway index dir (BOARD_TOKENS_DIR), throwaway port-assignments and
last-active files, TZ pinned to UTC+8 so day buckets are deterministic. Never
reads or writes the real ~/.claude or ~/.board-steward.

Run: python3 dev/test_958_tokens.py  -> exit 0 = green, 1 = a fail.
"""
from __future__ import annotations
import json, os, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_fails = 0
def check(cond, msg):
    global _fails
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond: _fails += 1

def near(a, b, eps=1e-9):
    return abs((a or 0) - (b or 0)) <= eps

TMP = Path(tempfile.mkdtemp(prefix="wb958-")).resolve()
PROJ = TMP / "projects"; PROJ.mkdir()
IDX = TMP / "tokens"
BOARDP = TMP / "work" / "Alpha"          # a registered board project
(BOARDP / "board").mkdir(parents=True)
(BOARDP / "board" / "board.json").write_text(json.dumps(
    {"cards": [{"num": 12, "title": "Ship the widget"}, {"num": 7, "title": "Old card"}]}))
FOLDER_BASE = TMP / "desk"               # a non-board folder base
(FOLDER_BASE / "Beta" / ".git").mkdir(parents=True)
(TMP / "assign.json").write_text(json.dumps({str(BOARDP / "board"): 7999}))
os.environ.update({
    "CLAUDE_PROJECTS_DIR": str(PROJ), "BOARD_TOKENS_DIR": str(IDX),
    "BOARD_ASSIGNMENTS": str(TMP / "assign.json"), "BOARD_ACTIVE": str(TMP / "last-active"),
    "TZ": "Asia/Singapore",
})
time.tzset()
IDX.mkdir()
(IDX / "folders.json").write_text(json.dumps({"bases": [str(FOLDER_BASE)]}))

import _tokens          # noqa: E402
import _tokens_report   # noqa: E402

SLUG = PROJ / "-slug"
SLUG.mkdir()
S1, S2, S3, S4, S5, S6 = (f"{i:08d}-0000-0000-0000-000000000000" for i in range(1, 7))


def asst(mid, ts, model="claude-opus-5-5", inp=0, out=0, cr=0, w5=0, w1=0, stop="end_turn",
         cwd="/nowhere", tools=(), text="", side=False, speed=None, sid=S1, ts_split=True):
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
         "cache_creation_input_tokens": w5 + w1}
    if ts_split:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}
    if speed:
        u["speed"] = speed
    content = [{"type": "text", "text": text}] if text else []
    for name, tid, tin in tools:
        content.append({"type": "tool_use", "id": tid, "name": name, "input": tin})
    return {"type": "assistant", "timestamp": ts, "sessionId": sid, "cwd": cwd,
            "isSidechain": side, "entrypoint": "cli",
            "message": {"id": mid, "model": model, "usage": u, "stop_reason": stop,
                        "content": content}}


def user(ts, text="hi", origin="human", cwd="/nowhere", sid=S1):
    return {"type": "user", "timestamp": ts, "sessionId": sid, "cwd": cwd,
            "origin": {"kind": origin}, "message": {"role": "user", "content": text}}


def result(ts, tid, chars, run=None, sid=S1):
    d = {"type": "user", "timestamp": ts, "sessionId": sid,
         "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                  "content": "x" * chars}]}}
    if run:
        d["toolUseResult"] = {"runId": run, "status": "async_launched"}
    return d


def write(path: Path, lines, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as fh:
        for l in lines:
            fh.write(json.dumps(l) + "\n")


def q(sql, *a):
    con = _tokens.connect()
    try:
        return con.execute(sql, a).fetchall()
    finally:
        con.close()


def test_pricing():
    print("pricing")
    cp = _tokens.cost_parts("claude-opus-5-5", 100, 1000, 10_000, 1000, 2000, 0)
    want = (100 * 4 + 1000 * 20 + 10_000 * 0.2 + 1000 * 4 * 1.25 + 2000 * 4 * 2) / 1e6
    check(near(sum(cp.values()), want), "opus-5-5: input, output, read, 5m x1.25, 1h x2")
    fast = _tokens.cost_parts("claude-opus-5-5", 100, 1000, 10_000, 1000, 2000, 0, "fast")
    check(near(sum(fast.values()), 2 * want), "fast mode doubles the price")
    dated = _tokens.cost_parts("claude-haiku-4-5-20251001", 1_000_000, 0, 0, 0, 0, 0)
    check(near(dated["inp"], 1.0), "dated model id maps to its base price")
    short = _tokens.cost_parts("claude-haiku-5-5", 1_000_000, 0, 0, 0, 0, 0)
    check(near(short["inp"], 0.5), "haiku-5-5 above 100K prompt uses the long rate")
    small = _tokens.cost_parts("claude-haiku-5-5", 50_000, 0, 0, 0, 0, 0)
    check(near(small["inp"], 50_000 * 0.1 / 1e6), "haiku-5-5 under 100K uses the base rate")
    check(_tokens.cost_parts("claude-mystery-9", 1, 1, 1, 1, 1, 0) is None, "unknown model is unpriced")
    check(sum(_tokens.cost_parts("<synthetic>", 5, 5, 5, 5, 5, 0).values()) == 0, "<synthetic> is free")
    check(near(_tokens.cost_parts("claude-sonnet-5-5", 0, 0, 0, 0, 0, 3)["web"], 0.03),
          "web search $0.01 each")


def build_fixture():
    alpha = str(BOARDP)
    # S1: main session in ~ (no board cwd), last-active maps it to Alpha.
    write(SLUG / f"{S1}.jsonl", [
        user("2026-10-28T15:50:00Z"),
        # one reply split over two lines: counted once
        asst("m1", "2026-10-28T15:59:59Z", inp=10, out=100, cr=50_000, w1=20_000, text="hello"),
        asst("m1", "2026-10-28T15:59:59Z", inp=10, out=100, cr=50_000, w1=20_000,
             tools=[("Agent", "tu_agent", {"prompt": "go"})]),
        user("2026-10-28T16:00:30Z", origin="task-notification"),
        asst("m2", "2026-10-28T16:00:31Z", inp=5, out=50, cr=70_000,
             tools=[("Workflow", "tu_wf", {"script": "s"})]),
        result("2026-10-28T16:00:32Z", "tu_wf", 10, run="wf_1"),
        asst("syn", "2026-10-28T16:00:33Z", model="<synthetic>"),
        asst("unk", "2026-10-28T16:00:34Z", model="claude-mystery-9", inp=1000, out=10),
        user("2026-10-28T16:01:00Z"),
        asst("m3", "2026-10-28T16:01:01Z", inp=1, out=10, cr=70_000),     # chat turn
        asst("m4", "2026-10-28T16:02:00Z", inp=1, out=10, cr=70_000,
             tools=[("Bash", "tu_b1", {"command": f"python3 card.py --board {alpha}/board/board.json fly 12 inprogress 2>&1"})]),
        result("2026-10-28T16:02:01Z", "tu_b1", 2370),
        asst("m5", "2026-10-28T16:02:02Z", inp=1, out=10, cr=71_000),
    ])
    # Sub-agent of S1: partial usage (old CLI), cwd inside the board project.
    write(SLUG / S1 / "subagents" / "agent-a1.jsonl", [
        asst("s1", "2026-10-28T16:00:10Z", inp=3, out=1, cr=40_000, w5=5000, stop=None,
             cwd=alpha + "/src", side=True, text="y" * 200,
             tools=[("Read", "tu_r", {"file_path": alpha + "/src/a.py"})]),
    ])
    (SLUG / S1 / "subagents" / "agent-a1.meta.json").write_text(json.dumps({"toolUseId": "tu_agent"}))
    # Workflow agent of S1 + a journal that must be ignored.
    write(SLUG / S1 / "subagents" / "workflows" / "wf_1" / "agent-b1.jsonl", [
        asst("w1", "2026-10-28T16:00:40Z", model="claude-sonnet-5-5", inp=2, out=300, cr=30_000,
             w5=12_000, side=True),
    ])
    write(SLUG / S1 / "subagents" / "workflows" / "wf_1" / "journal.jsonl", [
        asst("journal", "2026-10-28T16:00:41Z", inp=10**9)])
    # S2: a resumed copy of S1 holding m1 again (cross-file duplicate).
    write(SLUG / f"{S2}.jsonl", [
        user("2026-10-28T17:00:00Z", sid=S2),
        asst("m1", "2026-10-28T15:59:59Z", inp=10, out=100, cr=50_000, w1=20_000, sid=S2),
        asst("r2", "2026-10-28T17:00:01Z", inp=1, out=1, cr=1000, sid=S2, cwd=alpha),
    ])
    # S3: plain session at ~, touched paths in a non-board folder (rule 4b).
    beta = str(FOLDER_BASE / "Beta")
    write(SLUG / f"{S3}.jsonl", [
        user("2026-10-28T18:00:00Z", sid=S3),
        asst("t1", "2026-10-28T18:00:01Z", inp=1, out=1, cr=1000, sid=S3,
             tools=[("Edit", f"e{i}", {"file_path": f"{beta}/f{i}.py"}) for i in range(3)]),
    ])
    # S4: headless child of S3 (scratchpad cwd names its parent).
    write(SLUG / f"{S4}.jsonl", [
        asst("h1", "2026-10-28T18:05:00Z", inp=1, out=1, cr=1000, sid=S4,
             cwd=f"/private/tmp/claude-501/-slug/{S3}/scratchpad/x"),
    ])
    # S5: nothing to go on -> unattributed.
    write(SLUG / f"{S5}.jsonl", [
        asst("u1", "2026-10-28T18:10:00Z", inp=1, out=1, cr=1_000_000, sid=S5)])
    (TMP / "last-active").write_text(json.dumps(
        {"global": {}, "sessions": {S1: {"board": str(BOARDP / "board"), "at": "x"}}}))


def test_index():
    print("collector + attribution")
    build_fixture()
    st = _tokens.refresh()
    check(st["newReplies"] >= 10, f"first scan reads the fixture ({st['newReplies']} replies)")
    mids = {r[0] for r in q("SELECT mid FROM replies")}
    check("journal" not in mids, "workflow journal.jsonl is skipped")
    check(len(q("SELECT 1 FROM replies WHERE mid='m1'")) == 1, "a reply split over 2 lines is one row")
    fid = q("SELECT file_id FROM replies WHERE mid='m1'")[0][0]
    path = q("SELECT path FROM files WHERE id=?", fid)[0][0]
    check(path.endswith(f"{S1}.jsonl"), "cross-file duplicate keeps the originating file's copy")
    m1 = q("SELECT cost FROM replies WHERE mid='m1'")[0][0]
    want = (10 * 4 + 100 * 20 + 50_000 * 0.2 + 20_000 * 4 * 2) / 1e6
    check(near(m1, want), "m1 priced once with the 1h write rate")
    syn = q("SELECT cost, priced FROM replies WHERE mid='syn'")[0]
    check(syn[0] == 0 and syn[1] == 1, "<synthetic> row costs 0")
    unk = q("SELECT cost, priced, inp FROM replies WHERE mid='unk'")[0]
    check(unk[1] == 0 and unk[0] == 0 and unk[2] == 1000, "unknown model: tokens kept, flagged unpriced")
    s1 = q("SELECT out_raw, out, out_est, think FROM replies WHERE mid='s1'")[0]
    est, think = _tokens.estimate_output("claude-opus-5-5", 1, 200,
                                         len(json.dumps({"file_path": str(BOARDP) + "/src/a.py"})))
    check(s1[2] == 1 and s1[1] == est and s1[0] == 1 and s1[3] == think,
          f"partial-usage reply: output estimated ({s1[0]} -> {s1[1]}) and flagged")
    days = dict(q("SELECT mid, day FROM replies WHERE mid IN ('m1', 'm2')"))
    check(days["m1"] == "2026-10-28" and days["m2"] == "2026-10-29",
          "day buckets in local time (UTC+8): 15:59:59Z vs 16:00:31Z")
    rules = dict(q("SELECT mid, project_rule FROM replies"))
    projs = dict(q("SELECT mid, project FROM replies"))
    check(rules["s1"] == "cwd" and projs["s1"] == str(BOARDP), "rule 0: reply cwd in a board project")
    check(rules["m3"] == "last-active" and projs["m3"] == str(BOARDP), "rule 2: last-active session map")
    check(rules["w1"] == "last-active", "workflow agent inherits its session's project")
    check(rules["r2"] == "cwd", "resumed session reply with a board cwd")
    check(rules["t1"] == "paths-folder" and projs["t1"] == str(FOLDER_BASE / "Beta"),
          "rule 4b: touched-path vote names a non-board folder project")
    check(rules["h1"] == "paths-folder" and projs["h1"] == str(FOLDER_BASE / "Beta"),
          "headless child inherits its parent tree's project")
    check(rules["u1"] == "none" and projs["u1"] is None, "rule 5: unattributed")
    kinds = dict(q("SELECT mid, kind FROM replies"))
    check(kinds["s1"] == "sub-agent" and kinds["w1"] == "workflow-agent"
          and kinds["h1"] == "headless-child" and kinds["m1"] == "main-interactive",
          "reply kinds: main, sub-agent, workflow-agent, headless-child")
    cards = dict(q("SELECT mid, card FROM replies"))
    crule = dict(q("SELECT mid, card_rule FROM replies"))
    check(cards["m3"] is None, "no card before the first card.py call")
    check(cards["m5"] == f"{BOARDP}/board#12" and crule["m5"] == "backfill",
          "backfill: card.py fly 12 in a Bash call attributes later replies")
    # Turns: S1 m1 (Agent) and m2 (Workflow) delegate, m3-m5 run Bash; S2's
    # turn has no tools (chat); S3 edits.
    con = _tokens.connect()
    o = _tokens_report.compute(con, since_days=3650)
    con.close()
    cls = {c["key"]: c for c in o["turnClasses"]}
    check(cls.get("bash", {}).get("turns") == 1 and cls.get("editing", {}).get("turns") == 1
          and o["chatVsAction"]["chat"]["turns"] == 1,
          "turn classes: a Bash turn is an action turn, a tool-less turn is chat")
    check("delegating" in cls and cls["delegating"]["children"] > 0,
          "delegating turns carry their agents' cost (Agent toolUseId + Workflow runId)")
    trig = {t["key"] for t in o["turnTriggers"]}
    check({"human", "task-notification"} <= trig, "task-notification is its own turn trigger")
    check(o["totals"]["unpricedReplies"] == 1, "report counts the unpriced reply")
    check(o["totals"]["estimated"]["replies"] == 1, "report labels the estimated reply")
    rent = {x["key"]: x for x in o["contextRent"]["tool"]["top"]}
    check("Bash" in rent and near(rent["Bash"]["tokens"], 1000, 1),
          "context rent: Bash result tokens from chars / 2.37")
    check(o["confidence"]["project"]["unattributed"]["cost"] > 0, "confidence split reports unattributed")
    total_daily = sum(r[0] for r in q("SELECT cost FROM daily"))
    total_rep = sum(r[0] for r in q("SELECT cost FROM replies"))
    check(near(total_daily, total_rep, 1e-9), "daily rollup equals the reply rows")
    check(near(o["totals"]["cost"], total_rep, 0.006), "report total equals the index")
    return total_rep


def test_incremental(before):
    print("incremental re-index")
    st = _tokens.refresh()
    check(st["newReplies"] == 0 and st["dirtyFiles"] == 0, "re-run with no new bytes reads nothing")
    p = SLUG / f"{S1}.jsonl"
    write(p, [asst("m6", "2026-10-28T16:10:00Z", inp=1, out=10, cr=72_000)], mode="a")
    with open(p, "a") as fh:                       # a half-written line: not consumed yet
        fh.write(json.dumps(asst("m7", "2026-10-28T16:11:00Z", inp=1, out=10, cr=72_000))[:40])
    st = _tokens.refresh()
    after = sum(r[0] for r in q("SELECT cost FROM replies"))
    m6 = q("SELECT cost FROM replies WHERE mid='m6'")[0][0]
    check(st["newReplies"] == 1 and near(after, before + m6), "appended reply added once, no double count")
    check(not q("SELECT 1 FROM replies WHERE mid='m7'"), "a partial trailing line waits for its newline")
    with open(p, "a") as fh:
        fh.write(json.dumps(asst("m7", "2026-10-28T16:11:00Z", inp=1, out=10, cr=72_000))[40:] + "\n")
    _tokens.refresh()
    check(len(q("SELECT 1 FROM replies WHERE mid='m7'")) == 1, "completed line is picked up next scan")
    check(q("SELECT turn_id FROM replies WHERE mid='m7'")[0][0]
          == q("SELECT turn_id FROM replies WHERE mid='m5'")[0][0],
          "turn state carries across scans")
    total = sum(r[0] for r in q("SELECT cost FROM daily"))
    os.remove(SLUG / f"{S5}.jsonl")
    _tokens.refresh()
    check(near(total, sum(r[0] for r in q("SELECT cost FROM daily"))),
          "a transcript deleted by Claude Code keeps its rows and rollup")


def test_card_events():
    print("card event log")
    import card_commands as cc
    os.environ["CLAUDE_CODE_SESSION_ID"] = S1
    os.environ.pop("BOARD_SKIP_DECOMPOSE_CHECK", None)
    d = {"cards": [{"id": "c-w", "num": 7, "column": "task"}], "activeWork": None}
    cc._PENDING_CARD_EVENTS.clear()
    cc._set_active_work(d, d["cards"][0], "task", "inprogress", via="fly")
    check([e["event"] for e in cc._PENDING_CARD_EVENTS] == ["start"], "entering In Progress queues start")
    check(not _tokens.card_events_path().exists(), "nothing is written before the commit")
    cc.flush_card_events(BOARDP / "board" / "board.json")
    ev = [json.loads(l) for l in _tokens.card_events_path().read_text().splitlines()]
    check(len(ev) == 1 and ev[0]["sessionId"] == S1 and ev[0]["num"] == 7
          and ev[0]["board"] == str(BOARDP / "board") and ev[0]["via"] == "fly",
          "flush writes {at, sessionId, board, cardId, num, event, via}")
    os.environ["CLAUDE_CODE_SESSION_ID"] = "_cli"
    cc._set_active_work(d, d["cards"][0], "inprogress", "done", via="fly")
    check(not cc._PENDING_CARD_EVENTS, "bare CLI runs (no transcript) log nothing")
    # A log event at 16:05Z switches S1 to card 7 (exact) after the backfill's 12.
    with open(_tokens.card_events_path(), "a") as fh:
        fh.write(json.dumps({"at": "2026-10-28T16:05:00Z", "sessionId": S1,
                             "board": str(BOARDP / "board"), "num": 7, "event": "start"}) + "\n")
    _tokens.refresh()
    cards = dict(q("SELECT mid, card FROM replies"))
    rules = dict(q("SELECT mid, card_rule FROM replies"))
    check(cards["m6"] == f"{BOARDP}/board#7" and rules["m6"] == "log",
          "event log wins after its timestamp (exact)")
    check(cards["m5"] == f"{BOARDP}/board#12" and rules["m5"] == "backfill",
          "earlier replies keep the backfill card (inferred)")
    con = _tokens.connect()
    o = _tokens_report.compute(con, since_days=3650)
    con.close()
    c7 = next(c for c in o["byCard"] if c["key"] == f"{BOARDP}/board#7")
    check(c7["title"] == "Old card" and c7["confidence"]["exact"] > 0, "card row has title + confidence")
    txt = _tokens_report.to_text(o)
    check("ESTIMATED" in txt and "unpriced" in txt, "text report labels estimates and unpriced models")
    check("\u2014" not in txt, "no em dash in the report text")


def test_endpoint():
    print("GET /tokens")
    os.environ["BOARD_TOKENS_DISABLE"] = "1"   # no background scan thread here
    import serve

    class Cap:
        def __call__(self, status, body, ctype="application/json", extra=None):
            self.status, self.body = status, body

    def get(path):
        cap = Cap()
        h = serve.BoardHandler.__new__(serve.BoardHandler)
        h.board_dir = BOARDP / "board"
        h.path = path
        h._send = cap
        h._handle_tokens()
        return cap.status, json.loads(cap.body)

    st, all_ = get("/tokens?since=3650d")
    check(st == 200 and all_["sinceDays"] == 3650 and all_["project"] is None,
          "200 with the all-projects report")
    st, one = get("/tokens?since=3650&scope=board")
    check(st == 200 and one["project"] == str(BOARDP) and one["totals"]["cost"] < all_["totals"]["cost"],
          "scope=board narrows to this board's project")
    st, bad = get("/tokens?since=abc")
    check(st == 200 and bad["sinceDays"] == 7, "a bad ?since falls back to 7 days")


def test_ui_markup():
    print("board.html Tokens view")
    html = (REPO / "templates" / "board.html").read_text()
    check('data-view="tokens"' in html and '<div id="tokens"></div>' in html,
          "view toggle has a Tokens button and a #tokens container")
    check("renderTokens()" in html and "apiBase + '/tokens?since='" in html,
          "render() drives renderTokens, which reads the active board's /tokens")
    check(html.count("calc(100% - var(--hdr-h, 48px))") == 3,
          "board, calendar and tokens views size from the live header height")


def main():
    test_pricing()
    before = test_index()
    test_incremental(before)
    test_card_events()
    test_endpoint()
    test_ui_markup()
    print("\nFAIL" if _fails else "\nPASS", f"({_fails} failing)")
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
