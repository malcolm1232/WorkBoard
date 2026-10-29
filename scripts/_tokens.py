"""Token consumption + API-equivalent cost index (#958).

Reads Claude Code transcripts (~/.claude/projects/**/*.jsonl, sub-agent and
workflow-agent files included) into an incremental SQLite index under
~/.board-steward/tokens/, then attributes every reply to a project, a session
tree, a turn and a card. Reporting lives in _tokens_report.py; this module only
collects, prices and attributes. stdlib-only.

Design notes:
- One row per unique API reply (message.id). A reply is written as one line per
  content block, all carrying identical usage, so lines are never summed.
- Files are read from a saved byte offset up to the last complete newline; a
  shrunk file or changed inode is rescanned from 0. Files Claude Code deletes
  (it keeps ~30 days) keep their rows, and the `daily` rollup outlives the
  reply rows, so history survives transcript deletion.
- Sub-agent / workflow transcripts written by CLI <= 2.1.306 carry the usage
  from the START of the stream (stop_reason null, output a placeholder). Input
  and cache fields are right; output is estimated from the reply's text and
  tool-input size and the row is flagged out_est=1. Every report labels it.
- Day buckets use the machine's local timezone at index time (UTC is stored).
- No conversation text is stored: only lengths, counts, tool names and paths
  needed for attribution.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import sqlite3
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: scans are serialised by sqlite's own locking
    fcntl = None

# ===== prices =====
# $ per million tokens: (input, output, cache read). Cache writes are priced off
# input: 5-minute TTL x1.25, 1-hour TTL x2. Source: Anthropic API list prices as
# given for card #958, checked 29 Oct 2026. Dated ids (claude-x-YYYYMMDD) map to
# their base id. Anything else is counted in tokens and reported as UNPRICED.
PRICES_AS_OF = "2026-10-29"
PRICES = {
    "claude-fable-5-1":  (10.0, 50.0, 0.25),
    "claude-fable-5":    (10.0, 50.0, 1.0),
    "claude-opus-5-5":   (4.0, 20.0, 0.20),
    "claude-opus-5":     (5.0, 25.0, 0.5),
    "claude-opus-4-8":   (5.0, 25.0, 0.5),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5":   (2.0, 10.0, 0.2),
    "claude-haiku-5-5":  (0.10, 0.50, 0.01),
    "claude-haiku-4-5":  (1.0, 5.0, 0.1),
}
# Haiku 5.5 above 100K prompt tokens. The long-context cache-read rate is an
# ASSUMPTION (scaled 5x like input); see the spec's open questions.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}
WRITE_5M, WRITE_1H = 1.25, 2.0
FAST_MULT = 2.0          # usage.speed == "fast"
WEB_SEARCH_USD = 0.01    # per search; web fetch has no fee
SYNTHETIC = "<synthetic>"

# Output estimator for partial-usage replies: (chars per visible output token,
# mean thinking tokens per reply), fitted on clean sub-agent replies.
OUT_COEF = {
    "claude-opus-5-5": (2.00, 153),
    "claude-sonnet-5-5": (2.02, 198),
    "claude-fable-5-1": (1.91, 299),
}
OUT_COEF_DEFAULT = (2.0, 180)
CHARS_PER_TOKEN_CONTEXT = 2.37   # big tool results / injections (measured)
BREAK_MIN_PROMPT = 10_000
REPLY_RETENTION_DAYS = 90

_DATED = re.compile(r"-\d{8}$")


def norm_model(m: str | None) -> str:
    return _DATED.sub("", m or "")


def price_for(model: str, prompt_tokens: int):
    """(input, output, read) $/MTok for a model at a prompt size, or None."""
    m = norm_model(model)
    p = PRICES.get(m)
    if p is None:
        return None
    lp = LONG_PROMPT.get(m)
    if lp and prompt_tokens > lp[0]:
        return lp[1]
    return p


def cost_parts(model, inp, out, cr, w5, w1, ws, speed=None):
    """$ components of one reply as a dict, or None when the model is unpriced.
    `<synthetic>` (local placeholder replies) is free."""
    if model == SYNTHETIC:
        return dict(inp=0.0, out=0.0, cr=0.0, w5=0.0, w1=0.0, web=0.0)
    p = price_for(model, inp + cr + w5 + w1)
    if p is None:
        return None
    pi, po, pr = p
    k = FAST_MULT if speed == "fast" else 1.0
    return dict(inp=k * inp * pi / 1e6, out=k * out * po / 1e6, cr=k * cr * pr / 1e6,
                w5=k * w5 * pi * WRITE_5M / 1e6, w1=k * w1 * pi * WRITE_1H / 1e6,
                web=ws * WEB_SEARCH_USD)


def estimate_output(model, recorded, text_chars, tool_chars):
    """(output tokens, thinking tokens) for a partial-usage reply."""
    cpt, think = OUT_COEF.get(norm_model(model), OUT_COEF_DEFAULT)
    return max(recorded, int(round((text_chars + tool_chars) / cpt + think))), think


# ===== paths =====

def tokens_dir() -> Path:
    env = os.environ.get("BOARD_TOKENS_DIR")
    return Path(env).expanduser() if env else Path.home() / ".board-steward" / "tokens"


def projects_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECTS_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude" / "projects"


def card_events_path() -> Path:
    return tokens_dir() / "card_events.jsonl"


def folders_config_path() -> Path:
    return tokens_dir() / "folders.json"


# ===== card event log (written by card.py) =====

def append_card_events(events: list[dict]) -> None:
    """Append card events to card_events.jsonl. Best-effort: a failure here must
    never break the card write that produced them."""
    if not events:
        return
    try:
        p = card_events_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, sort_keys=True) + "\n")
    except Exception:
        pass


# ===== time helpers =====

def parse_ts(ts: str | None):
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def local_day_hour(ts: str):
    dt = parse_ts(ts)
    if dt is None:
        return None, None
    lt = dt.astimezone()
    return lt.strftime("%Y-%m-%d"), lt.hour


def _secs(a: str | None, b: str | None) -> float | None:
    da, db = parse_ts(a), parse_ts(b)
    if da is None or db is None:
        return None
    return (db - da).total_seconds()


# ===== classification =====

DELEGATE_TOOLS = {"Agent", "Task", "Workflow", "SendMessage", "TaskStop", "ListAgents"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
WEB_TOOLS = {"WebFetch", "WebSearch", "web_search", "web_fetch"}
BASH_TOOLS = {"Bash", "BashOutput", "Monitor", "KillShell"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "ToolSearch", "NotebookRead"}
SPAWN_TOOLS = {"Agent", "Task", "Workflow"}


def tool_kind(name: str) -> str:
    if name in DELEGATE_TOOLS:
        return "delegating"
    if name in EDIT_TOOLS:
        return "editing"
    if name.startswith("mcp__claude-in-chrome") or name.startswith("mcp__plugin_playwright") \
            or name == "computer":
        return "browser"
    if name in WEB_TOOLS:
        return "web"
    if name in BASH_TOOLS:
        return "bash"
    if name in READ_TOOLS:
        return "read"
    if name.startswith("mcp__"):
        return "mcp"
    return "meta"


# First match wins (spec 2.5). "chat" = a turn whose replies call no tool.
TURN_CLASS_ORDER = ["delegating", "editing", "browser", "web", "bash", "read", "mcp", "meta"]
TURN_CLASS_LABEL = {
    "chat": "chat (no tools)", "delegating": "delegating", "editing": "editing",
    "browser": "browser", "web": "web research", "bash": "bash only",
    "read": "read/search only", "mcp": "other MCP", "meta": "meta tools only",
}


def turn_class(tool_names) -> str:
    kinds = {tool_kind(t) for t in tool_names if t}
    if not kinds:
        return "chat"
    for k in TURN_CLASS_ORDER:
        if k in kinds:
            return k
    return "meta"


def _user_trigger(d: dict, content) -> str | None:
    """Turn trigger for a user line, or None when the line does not start a
    turn (a tool_result carrier or an isMeta line)."""
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    if d.get("isCompactSummary"):
        return "compaction-resume"
    o = d.get("origin")
    kind = o.get("kind") if isinstance(o, dict) else None
    if kind:
        return {"human": "human", "task-notification": "task-notification",
                "peer": "peer-message", "coordinator": "coordinator"}.get(kind, kind)
    if d.get("isMeta"):
        return None
    if isinstance(content, str):
        s = content
    elif isinstance(content, list):
        s = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    else:
        s = ""
    s = s.lstrip()[:40]
    if s.startswith("<command-") or s.startswith("<local-command"):
        return "command"
    if s.startswith("<task-notification"):
        return "task-notification"
    return "prompt"


def _content_chars(c) -> int:
    if c is None:
        return 0
    if isinstance(c, str):
        return len(c)
    n = 0
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                n += len(b.get("text") or "")
            elif t == "tool_result":
                n += _content_chars(b.get("content"))
            elif t not in ("image", "thinking", "redacted_thinking"):
                n += len(json.dumps(b))
    return n


# card.py <verb> <ref> in a Bash command (the transcript backfill, spec 3.2).
CARD_CALL = re.compile(
    r"card\.py\b[^|;&\n]*?\b(fly|move|bug|improve|update|subtask|review|auto-ship|claim)\b"
    r"([^|;&\n]{0,200})")
_CARD_REF = re.compile(r"#?(\d{1,6})")


def card_ref(verb: str, rest: str) -> str | None:
    """The card number a `card.py <verb> ...` call names: the first positional
    token (after the action word for `subtask`), skipping `--board PATH`.
    Non-numeric refs (codes, inbox ids) are not resolvable here -> None."""
    toks = rest.split()
    i = 0
    if verb == "subtask" and toks and not toks[0].startswith("-"):
        i = 1
    while i < len(toks):
        t = toks[i].strip("'\"")
        if t == "--board":
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        m = _CARD_REF.fullmatch(t)
        return m.group(1) if m else None
    return None
BOARD_ARG = re.compile(r"--board[ =]+['\"]?([^'\" ]+?)/board\.json")
CD_ABS = re.compile(r"(?:^|[;&|(]\s*)cd\s+(['\"]?)(/[^'\"\s;&|)]+)\1")
SCRATCH_PARENT = re.compile(
    r"/claude-\d+/[^/]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)")

# ===== schema =====

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, kind TEXT, session_id TEXT,
  agent_id TEXT, run_id TEXT, spawn_tool_use TEXT, agent_type TEXT,
  size INTEGER DEFAULT 0, offset INTEGER DEFAULT 0, inode INTEGER, lines INTEGER DEFAULT 0,
  first_ts TEXT, last_ts TEXT, cwds TEXT DEFAULT '{}', interactive INTEGER DEFAULT 0,
  parent_session TEXT, root_session TEXT, reply_kind TEXT,
  project TEXT, project_rule TEXT, state TEXT DEFAULT '{}', gone INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS files_session ON files(session_id);
CREATE INDEX IF NOT EXISTS files_root ON files(root_session);
CREATE TABLE IF NOT EXISTS replies(
  mid TEXT PRIMARY KEY, file_id INTEGER, line INTEGER, ts TEXT, day TEXT, hour INTEGER,
  model TEXT, effort TEXT, speed TEXT, stop TEXT, is_error INTEGER DEFAULT 0,
  inp INTEGER, out_raw INTEGER, out INTEGER, out_est INTEGER DEFAULT 0, think INTEGER,
  cr INTEGER, w5 INTEGER, w1 INTEGER, ws INTEGER, wf INTEGER,
  tools TEXT, text_chars INTEGER, tool_chars INTEGER, prompt INTEGER,
  priced INTEGER, cost REAL, cost_raw REAL,
  c_inp REAL, c_out REAL, c_cr REAL, c_w5 REAL, c_w1 REAL, c_web REAL,
  turn_id TEXT, break_cause TEXT, break_excess REAL DEFAULT 0,
  cwd_project TEXT, kind TEXT, project TEXT, project_rule TEXT, card TEXT, card_rule TEXT);
CREATE INDEX IF NOT EXISTS replies_day ON replies(day);
CREATE INDEX IF NOT EXISTS replies_file ON replies(file_id);
CREATE INDEX IF NOT EXISTS replies_turn ON replies(turn_id);
CREATE TABLE IF NOT EXISTS turns(
  turn_id TEXT PRIMARY KEY, file_id INTEGER, start_ts TEXT, day TEXT, trigger TEXT);
CREATE INDEX IF NOT EXISTS turns_file ON turns(file_id);
CREATE TABLE IF NOT EXISTS spawns(
  tool_use_id TEXT PRIMARY KEY, file_id INTEGER, mid TEXT, turn_id TEXT, name TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS wf_runs(run_id TEXT PRIMARY KEY, tool_use_id TEXT);
CREATE TABLE IF NOT EXISTS touches(file_id INTEGER, kind TEXT, key TEXT, n INTEGER,
  PRIMARY KEY(file_id, kind, key));
CREATE TABLE IF NOT EXISTS card_calls(file_id INTEGER, line INTEGER, ts TEXT, num TEXT,
  verb TEXT, board TEXT, cwd TEXT, PRIMARY KEY(file_id, line, num, verb));
CREATE TABLE IF NOT EXISTS card_log(session_id TEXT, ts TEXT, board TEXT, num TEXT,
  event TEXT, via TEXT);
CREATE INDEX IF NOT EXISTS card_log_sid ON card_log(session_id);
CREATE TABLE IF NOT EXISTS session_board(session_id TEXT PRIMARY KEY, board TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS rent(day TEXT, file_id INTEGER, kind TEXT, key TEXT,
  n INTEGER DEFAULT 0, tokens REAL DEFAULT 0, write_cost REAL DEFAULT 0, carry_cost REAL DEFAULT 0,
  PRIMARY KEY(day, file_id, kind, key));
CREATE TABLE IF NOT EXISTS compactions(file_id INTEGER, line INTEGER, ts TEXT, day TEXT,
  trigger TEXT, pre_tokens INTEGER, PRIMARY KEY(file_id, line));
CREATE TABLE IF NOT EXISTS daily(
  day TEXT, project TEXT, project_rule TEXT, card TEXT, card_rule TEXT, model TEXT, kind TEXT,
  n INTEGER, n_est INTEGER, n_unpriced INTEGER, n_error INTEGER,
  inp INTEGER, out INTEGER, out_raw INTEGER, think INTEGER, out_think_known INTEGER,
  cr INTEGER, w5 INTEGER, w1 INTEGER, ws INTEGER, wf INTEGER,
  cost REAL, cost_raw REAL, cost_est REAL,
  c_inp REAL, c_out REAL, c_cr REAL, c_w5 REAL, c_w1 REAL, c_web REAL, break_excess REAL);
CREATE INDEX IF NOT EXISTS daily_day ON daily(day);
"""

# Bump when the stored shape changes: the next refresh rebuilds from the
# transcripts (daily rows older than every retained reply are kept).
INDEX_VERSION = "2"
_VERSIONED_TABLES = ("card_calls",)   # tables whose columns changed since v1


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or (tokens_dir() / "index.sqlite")
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    v = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    if v is None:
        con.execute("INSERT INTO meta VALUES('version', ?)", (INDEX_VERSION,))
        con.commit()
    elif v[0] != INDEX_VERSION:
        for t in _VERSIONED_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.executescript(SCHEMA)
        con.execute("INSERT OR REPLACE INTO meta VALUES('version', ?)", (INDEX_VERSION,))
        con.execute("INSERT OR REPLACE INTO meta VALUES('needs_full', '1')")
        con.commit()
    return con


@contextlib.contextmanager
def _scan_lock(blocking: bool):
    """Cross-process lock so several board servers never scan at once."""
    p = tokens_dir() / "scan.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a+")
    try:
        if fcntl is None:
            yield True
            return
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


# ===== project roots =====

def board_roots() -> list[str]:
    """Project roots = parent of every registered board dir, longest first."""
    try:
        import port_registry
        boards = list(port_registry.assignments().keys())
    except Exception:
        boards = []
    return sorted({os.path.dirname(b.rstrip("/")) for b in boards if b}, key=len, reverse=True)


def root_of(path: str | None, roots: list[str]) -> str | None:
    if not path:
        return None
    for r in roots:
        if path == r or path.startswith(r + "/"):
            return r
    return None


def find_board_dir(cwd: str | None) -> str | None:
    """The board card.py finds from `cwd`: walk up (8 levels, like
    card_state.find_board) to the first <dir>/board/board.json."""
    if not cwd:
        return None
    cur = cwd.rstrip("/") or "/"
    for _ in range(8):
        if os.path.isfile(os.path.join(cur, "board", "board.json")):
            return os.path.join(cur, "board")
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def card_board(explicit: str | None, cwd: str | None, last_active: str | None) -> str | None:
    """Board dir a `card.py <verb> <n>` call acted on, resolved the way card.py
    does: --board, else walk up from its cwd, else the session's last-active
    board. None when none of them names an existing board."""
    for cand in (explicit, find_board_dir(cwd), last_active):
        if cand and os.path.isfile(os.path.join(cand, "board.json")):
            return cand.rstrip("/")
    return None


class BoardCards:
    """Card numbers + titles per board dir, from board.json and the monthly
    archives (archive_done.py moves old Done cards to board/archive/*.json)."""

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def titles(self, board: str) -> dict:
        if board not in self._cache:
            m: dict = {}
            files = [os.path.join(board, "board.json")]
            arch = os.path.join(board, "archive")
            if os.path.isdir(arch):
                files += sorted(os.path.join(arch, f) for f in os.listdir(arch) if f.endswith(".json"))
            for fp in files:
                try:
                    with open(fp, encoding="utf-8") as fh:
                        d = json.load(fh)
                except Exception:
                    continue
                cards = d.get("cards") if isinstance(d, dict) else d
                for c in cards or []:
                    if isinstance(c, dict) and c.get("num") is not None:
                        m.setdefault(str(c["num"]), c.get("title") or "")
            self._cache[board] = m
        return self._cache[board]

    def has(self, board: str, num) -> bool:
        return str(num) in self.titles(board)

    def title(self, board: str, num) -> str | None:
        return self.titles(board).get(str(num))


def clean_board_title(raw: str | None) -> str:
    """Project name from a board title, the same rule as board.html's
    cleanProjectTitle: drop a leading "WorkBoard -" or trailing "- Work Board"
    affix (any dash), never blanking a board literally named WorkBoard."""
    t = (raw or "").strip()
    s2 = re.sub(r"^\s*work\s*board\s*[\u2014\u2013-]\s*", "", t, flags=re.I)
    s2 = re.sub(r"\s*[\u2014\u2013-]\s*work\s*board\s*$", "", s2, flags=re.I).strip()
    return s2 or t


class FolderKeys:
    """Folder project key for a path outside any board (spec 3.1 rule 4b).

    The key is the first directory under a base dir (default: ~/Desktop,
    ~/Documents, ~/Projects, ~/code, ~/src, ~/dev, /Users/Shared) that is a git
    repo or a board project; failing that, a depth from folders.json `depth`
    ({"Work": 3}), else depth 1. folders.json `aliases` maps a key to a display
    name so many worktree folders can read as one project."""

    def __init__(self):
        home = str(Path.home())
        cfg = {}
        try:
            cfg = json.loads(folders_config_path().read_text())
        except Exception:
            cfg = {}
        bases = cfg.get("bases") or [f"{home}/{b}" for b in
                                     ("Desktop", "Documents", "Projects", "code", "src", "dev")] \
            + ["/Users/Shared"]
        self.bases = [b.rstrip("/") + "/" for b in bases]
        self.depth = {k: int(v) for k, v in (cfg.get("depth") or {}).items()}
        self.aliases = dict(cfg.get("aliases") or {})
        self._cache: dict[str, int] = {}

    def key(self, path: str) -> str | None:
        for base in self.bases:
            if not path.startswith(base):
                continue
            parts = [p for p in path[len(base):].split("/") if p]
            if len(parts) < 2:      # a file directly under the base is no project
                return None
            ck = base + parts[0]
            depth = self._cache.get(ck)
            if depth is None:
                depth = self.depth.get(parts[0]) or 0
                if not depth:
                    for i in (1, 2, 3):
                        d = base + "/".join(parts[:i])
                        if os.path.exists(d + "/.git") or os.path.exists(d + "/board/board.json"):
                            depth = i
                            break
                depth = depth or 1
                self._cache[ck] = depth
            if len(parts) <= depth:
                return None
            return base + "/".join(parts[:depth])
        return None


# ===== scanning =====

def _list_transcripts(root: Path) -> list[Path]:
    out = []
    if not root.exists():
        return out
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".jsonl") and f != "journal.jsonl":
                out.append(Path(dirpath) / f)
    return out


def _file_shape(root: Path, path: Path) -> dict:
    """kind / session / agent / run for a transcript path (spec 1.1)."""
    parts = path.relative_to(root).parts
    if "subagents" in parts:
        i = parts.index("subagents")
        session = parts[i - 1]
        agent = path.name[:-len(".jsonl")]
        if agent.startswith("agent-"):
            agent = agent[len("agent-"):]
        run = None
        kind = "sub-agent"
        if "workflows" in parts:
            kind = "workflow-agent"
            run = parts[parts.index("workflows") + 1]
        meta = {}
        mp = path.with_name(path.name[:-len(".jsonl")] + ".meta.json")
        try:
            meta = json.loads(mp.read_text())
        except Exception:
            meta = {}
        return dict(kind=kind, session_id=session, agent_id=agent, run_id=run,
                    spawn_tool_use=meta.get("toolUseId"), agent_type=meta.get("agentType"))
    return dict(kind="main", session_id=path.name[:-len(".jsonl")], agent_id=None,
                run_id=None, spawn_tool_use=None, agent_type=None)


class _FileScan:
    """Sequential per-file parser. Carries the small state that turn
    assignment, cache-break causes and context rent need across scans."""

    def __init__(self, ix, fid: int, frow, roots, folders):
        self.ix, self.fid, self.roots, self.folders = ix, fid, roots, folders
        self.kind = frow["kind"]
        st = json.loads(frow["state"] or "{}")
        self.turn_id = st.get("turn_id")
        self.turn_n = st.get("turn_n", 0)
        self.prev_ts = st.get("prev_ts")
        self.prev_model = st.get("prev_model")
        self.compact_since = st.get("compact_since", False)
        self.pending = st.get("pending", {})     # rent items not yet written: key -> [n, tokens]
        self.open = st.get("open", {})           # rent items in cache: key -> tokens
        self.tool_names = st.get("tool_names", {})  # tool_use_id -> name (bounded)
        self.cwds = json.loads(frow["cwds"] or "{}")
        self.first_ts, self.last_ts = frow["first_ts"], frow["last_ts"]
        self.interactive = frow["interactive"]
        self.line = frow["lines"] or 0
        self.replies: dict[str, dict] = {}
        self.order: list[str] = []
        self.turns, self.spawns, self.wf_runs, self.calls, self.comps = [], [], [], [], []
        self.touch: dict[tuple, int] = {}
        self.rent: dict[tuple, list] = {}

    def state(self) -> str:
        tn = self.tool_names
        if len(tn) > 400:   # tool results arrive right after their call; keep the tail
            tn = dict(list(tn.items())[-200:])
        return json.dumps(dict(turn_id=self.turn_id, turn_n=self.turn_n, prev_ts=self.prev_ts,
                               prev_model=self.prev_model, compact_since=self.compact_since,
                               pending=self.pending, open=self.open, tool_names=tn))

    # -- line handlers --
    def feed(self, raw: bytes):
        ln = self.line
        self.line += 1
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        t = d.get("type")
        ts = d.get("timestamp")
        if ts:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
        cwd = d.get("cwd")
        if cwd and t in ("assistant", "user"):
            self.cwds[cwd] = self.cwds.get(cwd, 0) + 1
        if d.get("entrypoint") == "cli":
            self.interactive = 1
        if t == "assistant":
            self._assistant(d, ln, ts)
        elif t == "user":
            self._user(d, ln, ts)
        elif t == "attachment":
            a = d.get("attachment") or {}
            chars = sum(len(x.get("content") or "") for x in (d.get("rendered") or [])
                        if isinstance(x, dict))
            if chars:
                key = a.get("type") or "?"
                cmd = a.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    key += ":" + os.path.basename(cmd.split()[0].strip("\"'"))[:40]
                self._rent_add("inject", key, chars)
        elif t == "system" and d.get("subtype") == "compact_boundary":
            cm = d.get("compactMetadata") or {}
            day, _h = local_day_hour(ts) if ts else (None, None)
            self.comps.append((self.fid, ln, ts, day, cm.get("trigger"), cm.get("preTokens")))
            self.compact_since = True
            self.open = {}
            self.pending = {}

    def _rent_add(self, kind, key, chars):
        k = kind + "\t" + key
        p = self.pending.setdefault(k, [0, 0.0])
        p[0] += 1
        p[1] += chars / CHARS_PER_TOKEN_CONTEXT

    def _user(self, d, ln, ts):
        m = d.get("message") or {}
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    nm = self.tool_names.get(tid) or "?"
                    if nm.startswith("mcp__"):
                        nm = "mcp:" + nm.split("__")[1]
                    self._rent_add("tool", nm, _content_chars(b.get("content")))
                    tur = d.get("toolUseResult")
                    if isinstance(tur, dict) and tur.get("runId") and tid:
                        self.wf_runs.append((tur["runId"], tid))
        if self.kind != "main":
            return
        trig = _user_trigger(d, c)
        if trig is None:
            return
        self.turn_n += 1
        self.turn_id = f"{self.fid}:{self.turn_n}"
        day, _h = local_day_hour(ts) if ts else (None, None)
        self.turns.append((self.turn_id, self.fid, ts, day, trig))

    def _assistant(self, d, ln, ts):
        m = d.get("message") or {}
        mid = m.get("id") or (("req:" + d["requestId"]) if d.get("requestId") else None)
        if not mid:
            return
        r = self.replies.get(mid)
        if r is None:
            u = m.get("usage") or {}
            cc = u.get("cache_creation") or {}
            stu = u.get("server_tool_use") or {}
            od = u.get("output_tokens_details") or {}
            cw = u.get("cache_creation_input_tokens") or 0
            w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
            if w5 is None and w1 is None:
                w5, w1 = cw, 0      # no TTL split on older lines: treat as 5m
            r = self.replies[mid] = dict(
                mid=mid, line=ln, ts=ts, model=m.get("model") or "", effort=d.get("effort"),
                speed=u.get("speed"), stop=m.get("stop_reason"),
                is_error=1 if (d.get("isApiErrorMessage") or d.get("apiError")) else 0,
                has_usage=bool(m.get("usage")),
                inp=u.get("input_tokens") or 0, out_raw=u.get("output_tokens") or 0,
                think=od.get("thinking_tokens"), cr=u.get("cache_read_input_tokens") or 0,
                w5=w5 or 0, w1=w1 or 0, ws=stu.get("web_search_requests") or 0,
                wf=stu.get("web_fetch_requests") or 0, tools=[], text_chars=0, tool_chars=0,
                cwd=d.get("cwd"), turn_id=self.turn_id, side=bool(d.get("isSidechain")))
            self.order.append(mid)
            self._sequence(r)
        elif r["stop"] is None and m.get("stop_reason") is not None:
            # A later line of the same reply carries the final stop/usage.
            u = m.get("usage") or {}
            r["stop"] = m.get("stop_reason")
            r["out_raw"] = max(r["out_raw"], u.get("output_tokens") or 0)
        for b in m.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                r["text_chars"] += len(b.get("text") or "")
            elif bt in ("tool_use", "server_tool_use", "mcp_tool_use"):
                self._tool_use(r, b, ln, ts)

    def _tool_use(self, r, b, ln, ts):
        nm = b.get("name") or "?"
        r["tools"].append(nm)
        inp = b.get("input") or {}
        r["tool_chars"] += len(json.dumps(inp))
        tid = b.get("id")
        if tid:
            self.tool_names[tid] = nm
            if nm in SPAWN_TOOLS and self.kind == "main":
                self.spawns.append((tid, self.fid, r["mid"], self.turn_id, nm, ts))
        if not isinstance(inp, dict):
            return
        for key in ("file_path", "path", "notebook_path"):
            v = inp.get(key)
            if isinstance(v, str) and v.startswith("/"):
                self._touch(v)
        cmd = inp.get("command")
        if isinstance(cmd, str):
            for rt in self.roots:
                if rt + "/" in cmd or cmd.endswith(rt):
                    self.touch[("board", rt)] = self.touch.get(("board", rt), 0) + 1
                    break
            for mm in CARD_CALL.finditer(cmd):
                num = card_ref(mm.group(1), mm.group(2))
                if not num:
                    continue
                # card.py resolves its board from --board, else by walking up
                # from its cwd, else from the session's last-active board.
                # Record the first two here; the walk and the last-active
                # fallback run at attribution time (card_board()).
                cwd = r.get("cwd")
                for cm in CD_ABS.finditer(cmd[:mm.start()]):
                    cwd = cm.group(2)
                bm = BOARD_ARG.search(cmd, mm.start(), mm.end() + len(mm.group(2)))
                board = None
                if bm:
                    bp = bm.group(1)
                    if not bp.startswith("/") and cwd:
                        bp = os.path.normpath(os.path.join(cwd, bp))
                    board = bp if bp.startswith("/") else None
                self.calls.append((self.fid, ln, ts, num, mm.group(1), board, cwd))

    def _touch(self, path):
        rt = root_of(path, self.roots)
        if rt:
            self.touch[("board", rt)] = self.touch.get(("board", rt), 0) + 1
            return
        fk = self.folders.key(path)
        if fk:
            self.touch[("folder", fk)] = self.touch.get(("folder", fk), 0) + 1

    def _sequence(self, r):
        """Cache-break cause + context rent, in line order (needs the previous
        reply of this file, carried in the per-file state)."""
        model = r["model"]
        if model == SYNTHETIC or not r["has_usage"]:
            return
        prompt = r["inp"] + r["cr"] + r["w5"] + r["w1"]
        r["prompt"] = prompt
        p = price_for(model, prompt) or (0.0, 0.0, 0.0)
        k = FAST_MULT if r["speed"] == "fast" else 1.0
        pin, pread = p[0] * k, p[2] * k
        writes = r["w5"] + r["w1"]
        if prompt > BREAK_MIN_PROMPT and writes > 0.5 * prompt:
            gap = _secs(self.prev_ts, r["ts"]) if self.prev_ts else None
            if self.prev_ts is None:
                cause = "cold start"
            elif self.compact_since:
                cause = "after compaction"
            elif self.prev_model and self.prev_model != model:
                cause = "model switch"
            elif gap is not None and gap > 3600:
                cause = "idle > 1h"
            elif gap is not None and gap > 300 and r["w1"] == 0:
                cause = "idle 5m-1h, 5m TTL"
            elif gap is not None and gap > 300:
                cause = "idle 5m-1h, 1h TTL (prefix change)"
            else:
                cause = "prefix change < 5m"
            r["break_cause"] = cause
            r["break_excess"] = (r["w5"] * (pin * WRITE_5M - pread)
                                 + r["w1"] * (pin * WRITE_1H - pread)) / 1e6
        # Context rent: everything already in cache is re-read by this reply;
        # pending items are written by it (then re-read by later replies).
        day, _h = local_day_hour(r["ts"]) if r["ts"] else (None, None)
        if day:
            wrate = pin * (WRITE_1H if r["w1"] > 0 else WRITE_5M)
            for kk, tok in self.open.items():
                g = self.rent.setdefault((day, kk), [0, 0.0, 0.0, 0.0])
                g[3] += tok * pread / 1e6
            for kk, (n, tok) in self.pending.items():
                g = self.rent.setdefault((day, kk), [0, 0.0, 0.0, 0.0])
                g[0] += n
                g[1] += tok
                g[2] += tok * wrate / 1e6
                self.open[kk] = self.open.get(kk, 0.0) + tok
            self.pending = {}
        self.prev_ts, self.prev_model, self.compact_since = r["ts"], model, False


class Index:
    def __init__(self, con: sqlite3.Connection | None = None):
        self.con = con or connect()

    def close(self):
        self.con.close()

    # ---- public entry point ----
    def refresh(self, full: bool = False, min_interval: float = 0.0,
                blocking: bool = True) -> dict:
        """Scan new transcript bytes, re-attribute dirty session trees, rebuild
        the daily rollup. Returns a small stats dict. Skips (returns
        {"skipped": ...}) when another process holds the scan lock and
        blocking=False, or when the last scan is younger than min_interval."""
        if self._meta("needs_full"):
            full = True
        last = self._meta("last_scan")
        if not full and min_interval and last and time.time() - float(last) < min_interval:
            return {"skipped": "fresh"}
        with _scan_lock(blocking) as got:
            if not got:
                return {"skipped": "locked"}
            t0 = time.time()
            if full:
                self._reset()
                self.con.execute("DELETE FROM meta WHERE key='needs_full'")
            stats = self._scan()
            stats["secs"] = round(time.time() - t0, 2)
            self._set_meta("last_scan", str(time.time()))
            self.con.commit()
            return stats

    def _meta(self, key):
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def _set_meta(self, key, value):
        self.con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))

    def _reset(self):
        """Full re-index: forget every file still on disk so it is re-read from
        byte 0. Files Claude Code has already deleted keep their rows (they
        cannot be re-read), and the rollup is rebuilt from the reply rows, so
        a reindex never loses history."""
        for r in self.con.execute("SELECT id, path FROM files").fetchall():
            if os.path.exists(r[1]):
                self._forget_file(r[0])
                self.con.execute("DELETE FROM files WHERE id=?", (r[0],))
        self.con.execute("DELETE FROM card_log")
        self.con.execute("DELETE FROM meta WHERE key IN ('card_log_offset', 'roots')")
        self.con.commit()

    # ---- scan ----
    def _scan(self) -> dict:
        root = projects_dir()
        roots = board_roots()
        roots_sig = json.dumps(roots)
        all_dirty = roots_sig != (self._meta("roots") or "")
        self._set_meta("roots", roots_sig)
        folders = FolderKeys()
        known = {r["path"]: r for r in self.con.execute("SELECT * FROM files")}
        seen = set()
        dirty_files: set[int] = set()
        new_replies = 0
        bytes_read = 0
        for path in _list_transcripts(root):
            sp = str(path)
            seen.add(sp)
            try:
                st = path.stat()
            except OSError:
                continue
            row = known.get(sp)
            if row is not None and row["size"] == st.st_size and row["inode"] == st.st_ino \
                    and not row["gone"]:
                continue
            if row is None:
                shape = _file_shape(root, path)
                cur = self.con.execute(
                    "INSERT INTO files(path, kind, session_id, agent_id, run_id, spawn_tool_use, "
                    "agent_type, inode) VALUES(?,?,?,?,?,?,?,?)",
                    (sp, shape["kind"], shape["session_id"], shape["agent_id"], shape["run_id"],
                     shape["spawn_tool_use"], shape["agent_type"], st.st_ino))
                row = self.con.execute("SELECT * FROM files WHERE id=?",
                                       (cur.lastrowid,)).fetchone()
            elif st.st_size < (row["offset"] or 0) or row["inode"] != st.st_ino:
                self._forget_file(row["id"])
                self.con.execute("UPDATE files SET offset=0, lines=0, size=0, state='{}', "
                                 "cwds='{}', first_ts=NULL, last_ts=NULL, inode=? WHERE id=?",
                                 (st.st_ino, row["id"]))
                row = self.con.execute("SELECT * FROM files WHERE id=?", (row["id"],)).fetchone()
            n, b = self._scan_file(path, row, st, roots, folders)
            new_replies += n
            bytes_read += b
            dirty_files.add(row["id"])
        for sp, row in known.items():
            if sp not in seen and not row["gone"]:
                self.con.execute("UPDATE files SET gone=1 WHERE id=?", (row["id"],))
        dirty_sessions = self._ingest_card_log()
        dirty_sessions |= self._snapshot_last_active()
        self.con.commit()
        changed = bool(dirty_files or dirty_sessions or all_dirty)
        if changed:
            self._attribute(None if all_dirty else dirty_files, dirty_sessions)
            self._prune_and_rollup()
        return {"files": len(seen), "dirtyFiles": len(dirty_files),
                "newReplies": new_replies, "bytesRead": bytes_read}

    def _forget_file(self, fid):
        for t in ("replies", "turns", "spawns", "touches", "card_calls", "compactions"):
            self.con.execute(f"DELETE FROM {t} WHERE file_id=?", (fid,))
        self.con.execute("DELETE FROM rent WHERE file_id=?", (fid,))

    def _scan_file(self, path, row, st, roots, folders):
        fs = _FileScan(self, row["id"], row, roots, folders)
        off = row["offset"] or 0
        consumed = 0
        # Stream line by line (transcripts reach 100s of MB). A trailing line
        # without its newline is still being written: leave it for next scan.
        with open(path, "rb") as fh:
            fh.seek(off)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                consumed += len(raw)
                if raw.strip():
                    fs.feed(raw)
                else:
                    fs.line += 1
        self._store(fs)
        self.con.execute(
            "UPDATE files SET size=?, offset=?, inode=?, lines=?, first_ts=?, last_ts=?, "
            "cwds=?, interactive=?, state=?, gone=0 WHERE id=?",
            (off + consumed, off + consumed, st.st_ino, fs.line, fs.first_ts, fs.last_ts,
             json.dumps(fs.cwds), fs.interactive, fs.state(), row["id"]))
        return len(fs.order), consumed

    def _store(self, fs: _FileScan):
        con = self.con
        mids = fs.order
        existing = {}
        for i in range(0, len(mids), 500):
            chunk = mids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in con.execute(f"SELECT mid, file_id, ts, kind FROM replies WHERE mid IN ({q})",
                                 chunk):
                existing[r["mid"]] = r
        in_side_file = fs.kind != "main"
        rows = []
        for mid in mids:
            r = fs.replies[mid]
            old = existing.get(mid)
            if old is not None:
                if old["file_id"] == fs.fid:
                    continue
                # Cross-file duplicate (resumed / forked session): keep the copy
                # in the file whose kind matches the reply's own isSidechain
                # flag (its originating file), else keep the first one seen.
                okind = con.execute("SELECT kind FROM files WHERE id=?",
                                    (old["file_id"],)).fetchone()
                old_own = bool(okind and okind[0] != "main") == r["side"]
                new_own = in_side_file == r["side"]
                if not (new_own and not old_own):
                    continue
            rows.append(self._reply_row(fs, r))
        con.executemany(
            "INSERT OR REPLACE INTO replies(mid, file_id, line, ts, day, hour, model, effort, speed, "
            "stop, is_error, inp, out_raw, out, out_est, think, cr, w5, w1, ws, wf, tools, "
            "text_chars, tool_chars, prompt, priced, cost, cost_raw, c_inp, c_out, c_cr, c_w5, "
            "c_w1, c_web, turn_id, break_cause, break_excess, cwd_project) "
            "VALUES(" + ",".join("?" * 38) + ")", rows)
        con.executemany("INSERT OR IGNORE INTO turns VALUES(?,?,?,?,?)", fs.turns)
        con.executemany("INSERT OR IGNORE INTO spawns VALUES(?,?,?,?,?,?)", fs.spawns)
        con.executemany("INSERT OR IGNORE INTO wf_runs VALUES(?,?)", fs.wf_runs)
        con.executemany("INSERT OR IGNORE INTO card_calls VALUES(?,?,?,?,?,?,?)", fs.calls)
        con.executemany("INSERT OR IGNORE INTO compactions VALUES(?,?,?,?,?,?)", fs.comps)
        con.executemany(
            "INSERT INTO touches VALUES(?,?,?,?) ON CONFLICT(file_id, kind, key) "
            "DO UPDATE SET n = n + excluded.n",
            [(fs.fid, k[0], k[1], n) for k, n in fs.touch.items()])
        con.executemany(
            "INSERT INTO rent VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(day, file_id, kind, key) "
            "DO UPDATE SET n=n+excluded.n, tokens=tokens+excluded.tokens, "
            "write_cost=write_cost+excluded.write_cost, carry_cost=carry_cost+excluded.carry_cost",
            [(day, fs.fid, kk.split("\t", 1)[0], kk.split("\t", 1)[1], g[0], g[1], g[2], g[3])
             for (day, kk), g in fs.rent.items()])

    def _reply_row(self, fs, r):
        roots = fs.roots
        model = r["model"]
        partial = r["stop"] is None and model != SYNTHETIC and r["has_usage"]
        out, think = r["out_raw"], r["think"]
        if partial:
            out, think = estimate_output(model, r["out_raw"], r["text_chars"], r["tool_chars"])
        cp = cost_parts(model, r["inp"], out, r["cr"], r["w5"], r["w1"], r["ws"], r["speed"])
        cpr = cost_parts(model, r["inp"], r["out_raw"], r["cr"], r["w5"], r["w1"], r["ws"],
                         r["speed"])
        day, hour = local_day_hour(r["ts"]) if r["ts"] else (None, None)
        priced = cp is not None
        cp = cp or dict(inp=0.0, out=0.0, cr=0.0, w5=0.0, w1=0.0, web=0.0)
        return (r["mid"], fs.fid, r["line"], r["ts"], day, hour, model, r["effort"], r["speed"],
                r["stop"], r["is_error"], r["inp"], r["out_raw"], out, 1 if partial else 0, think,
                r["cr"], r["w5"], r["w1"], r["ws"], r["wf"], ",".join(r["tools"]),
                r["text_chars"], r["tool_chars"], r["inp"] + r["cr"] + r["w5"] + r["w1"],
                1 if priced else 0, sum(cp.values()),
                sum(cpr.values()) if cpr else 0.0,
                cp["inp"], cp["out"], cp["cr"], cp["w5"], cp["w1"], cp["web"],
                r["turn_id"], r.get("break_cause"), r.get("break_excess", 0.0),
                root_of(r["cwd"], roots))

    # ---- card event log + last-active snapshot ----
    def _ingest_card_log(self) -> set:
        p = card_events_path()
        dirty = set()
        try:
            size = p.stat().st_size
        except OSError:
            return dirty
        off = int(self._meta("card_log_offset") or 0)
        if size < off:
            self.con.execute("DELETE FROM card_log")
            off = 0
        if size == off:
            return dirty
        with open(p, "rb") as fh:
            fh.seek(off)
            data = fh.read(size - off)
        end = data.rfind(b"\n")
        if end < 0:
            return dirty
        rows = []
        for raw in data[:end + 1].splitlines():
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict) or not e.get("sessionId"):
                continue
            num = e.get("num")
            rows.append((e["sessionId"], e.get("at"), e.get("board"),
                         str(num) if num is not None else None, e.get("event"), e.get("via")))
            dirty.add(e["sessionId"])
        self.con.executemany("INSERT INTO card_log VALUES(?,?,?,?,?,?)", rows)
        self._set_meta("card_log_offset", str(off + end + 1))
        return dirty

    def _snapshot_last_active(self) -> set:
        """last-active keeps only the newest sessions, so copy every entry we
        see into the index (rule 2 must not decay as the file is pruned)."""
        dirty = set()
        try:
            import port_registry
            sessions = port_registry._read_active().get("sessions") or {}
        except Exception:
            return dirty
        have = {r[0]: r[1] for r in self.con.execute("SELECT session_id, board FROM session_board")}
        for sid, e in sessions.items():
            if not isinstance(e, dict) or not e.get("board"):
                continue
            if have.get(sid) != e["board"]:
                self.con.execute("INSERT OR REPLACE INTO session_board VALUES(?,?,?)",
                                 (sid, e["board"], e.get("at")))
                dirty.add(sid)
        return dirty

    # ---- attribution ----
    def _attribute(self, dirty_files, dirty_sessions):
        con = self.con
        self.cards = BoardCards()
        roots = board_roots()
        files = {r["id"]: dict(r) for r in con.execute(
            "SELECT id, kind, session_id, cwds, interactive, first_ts, spawn_tool_use, run_id, "
            "root_session FROM files")}
        mains = {f["session_id"]: f for f in files.values() if f["kind"] == "main"}
        for f in files.values():
            if f["kind"] == "main":
                cw = json.loads(f["cwds"] or "{}")
                f["dom_cwd"] = max(cw, key=cw.get) if cw else None
                m = SCRATCH_PARENT.search((f["dom_cwd"] or "") + "/")
                f["parent"] = m.group(1) if m and m.group(1) != f["session_id"] else None
            else:
                f["parent"] = f["session_id"]

        def root(sid, depth=0):
            f = mains.get(sid)
            if f is None or not f.get("parent") or depth > 10 or f["parent"] not in mains:
                return sid
            return root(f["parent"], depth + 1)

        for f in files.values():
            f["root"] = root(f["session_id"]) if f["kind"] == "main" else root(f["parent"])
            if f["kind"] == "main":
                if f["parent"] and f["parent"] in mains:
                    f["reply_kind"] = "headless-child"
                else:
                    f["reply_kind"] = "main-interactive" if f["interactive"] else "main-headless"
            else:
                f["reply_kind"] = f["kind"]
        if dirty_files is None:
            todo_roots = {f["root"] for f in files.values()}
        else:
            todo_roots = {files[i]["root"] for i in dirty_files if i in files}
            todo_roots |= {f["root"] for f in files.values()
                           if f["session_id"] in dirty_sessions or f["root"] in dirty_sessions}
            # A root that moved (parent discovered later) leaves its old tree stale.
            todo_roots |= {f["root_session"] for i, f in files.items()
                           if i in dirty_files and f["root_session"]}
        if not todo_roots:
            return
        tree: dict[str, list] = {}
        for f in files.values():
            if f["root"] in todo_roots:
                tree.setdefault(f["root"], []).append(f)
        sess_board = {r[0]: r[1] for r in con.execute("SELECT session_id, board FROM session_board")}
        touches: dict[int, list] = {}
        for r in con.execute("SELECT file_id, kind, key, n FROM touches"):
            touches.setdefault(r[0], []).append((r[1], r[2], r[3]))
        spawn_ts = {r[0]: r[1] for r in con.execute("SELECT tool_use_id, ts FROM spawns")}
        run_tool = {r[0]: r[1] for r in con.execute("SELECT run_id, tool_use_id FROM wf_runs")}
        for rt, fl in tree.items():
            self._attribute_tree(rt, fl, mains, sess_board, touches, roots, spawn_ts, run_tool)
        con.commit()

    def _session_project(self, sid, mains, sess_board, roots):
        f = mains.get(sid)
        if f is not None:
            p = root_of(f.get("dom_cwd"), roots)
            if p:
                return p, "session-cwd"
        b = sess_board.get(sid)
        if b:
            return os.path.dirname(b.rstrip("/")), "last-active"
        return None, None

    def _attribute_tree(self, rt, fl, mains, sess_board, touches, roots, spawn_ts, run_tool):
        con = self.con
        # Project: own session, then the root of a headless chain, then the
        # touched-path vote over the whole tree, then unattributed.
        board_votes, folder_votes = {}, {}
        for f in fl:
            for kind, key, n in touches.get(f["id"], ()):
                tgt = board_votes if kind == "board" else folder_votes
                tgt[key] = tgt.get(key, 0) + n
        vote = None
        for votes, rule in ((board_votes, "paths-board"), (folder_votes, "paths-folder")):
            if votes:
                k, n = max(votes.items(), key=lambda kv: kv[1])
                if n >= 3:
                    vote = (k, rule)
                    break
        root_proj = self._session_project(rt, mains, sess_board, roots)
        # Card stream for the tree: the card.py event log (exact) merged with
        # card.py calls found in the transcripts (backfill, inferred).
        sids = {f["session_id"] for f in fl}
        events = []
        q = ",".join("?" * len(sids))
        for r in con.execute(f"SELECT ts, board, num, event FROM card_log WHERE session_id IN ({q}) "
                             "AND event IN ('start', 'touch', 'stop')", list(sids)):
            if r[0] and r[2]:
                ok = r[1] and self.cards.has(r[1], r[2])
                events.append((r[0], r[1] if ok else None, r[2] if ok else None,
                               "log" if ok else None, None))
        fids = [f["id"] for f in fl]
        fsid = {f["id"]: f["session_id"] for f in fl}
        q = ",".join("?" * len(fids))
        for r in con.execute(f"SELECT ts, board, num, file_id, cwd FROM card_calls "
                             f"WHERE file_id IN ({q})", fids):
            if not r[0]:
                continue
            # A call whose board cannot be resolved, or whose number is not a
            # card on that board, ends the previous card without naming one:
            # counted as no card rather than guessed.
            board = card_board(r[1], r[4], sess_board.get(fsid[r[3]]))
            ok = board is not None and self.cards.has(board, r[2])
            events.append((r[0], board if ok else None, r[2] if ok else None,
                           "backfill" if ok else None, r[3]))
        events.sort(key=lambda e: e[0])
        ev_ts = [e[0] for e in events]
        for f in fl:
            proj, rule = self._session_project(f["session_id"], mains, sess_board, roots)
            if proj is None and root_proj[0]:
                proj, rule = root_proj[0], "parent"
            if proj is None and vote:
                proj, rule = vote
            if proj is None:
                rule = "none"
            con.execute("UPDATE files SET root_session=?, parent_session=?, reply_kind=?, project=?, "
                        "project_rule=? WHERE id=?",
                        (rt, f["parent"], f["reply_kind"], proj, rule, f["id"]))
            con.execute(
                "UPDATE replies SET kind=?, "
                "project=CASE WHEN cwd_project IS NOT NULL THEN cwd_project ELSE ? END, "
                "project_rule=CASE WHEN cwd_project IS NOT NULL THEN 'cwd' ELSE ? END "
                "WHERE file_id=?", (f["reply_kind"], proj, rule, f["id"]))
            if not events:
                con.execute("UPDATE replies SET card=NULL, card_rule=NULL WHERE file_id=?",
                            (f["id"],))
                continue
            own = [e for e in events if e[4] == f["id"]] if f["kind"] != "main" else []
            anchor = None
            if f["kind"] != "main":
                tu = f.get("spawn_tool_use") or run_tool.get(f.get("run_id"))
                anchor = spawn_ts.get(tu) if tu else None
                anchor = anchor or f.get("first_ts")
            ups = []
            for r in con.execute("SELECT mid, ts FROM replies WHERE file_id=?", (f["id"],)):
                ts = r[1] or ""
                e = None
                if own:
                    cand = [x for x in own if x[0] <= ts]
                    e = cand[-1] if cand else None
                if e is None:
                    t = anchor if anchor else ts
                    i = _bisect_right(ev_ts, t) - 1
                    e = events[i] if i >= 0 else None
                if e is None or e[1] is None:
                    ups.append((None, None, r[0]))
                    continue
                ups.append((f"{e[1]}#{e[2]}", e[3], r[0]))
            con.executemany("UPDATE replies SET card=?, card_rule=? WHERE mid=?", ups)

    # ---- retention + rollup ----
    def _prune_and_rollup(self):
        con = self.con
        cutoff = (datetime.date.today() - datetime.timedelta(days=REPLY_RETENTION_DAYS)).isoformat()
        con.execute("DELETE FROM replies WHERE day < ?", (cutoff,))
        con.execute("DELETE FROM turns WHERE day < ?", (cutoff,))
        oldest = con.execute("SELECT MIN(day) FROM replies").fetchone()[0]
        if oldest is None:
            con.commit()
            return
        # Days that still have reply rows are rebuilt; older days stay frozen
        # (their transcripts may already be deleted).
        con.execute("DELETE FROM daily WHERE day >= ?", (oldest,))
        con.execute("""
            INSERT INTO daily SELECT day, project, project_rule, card, card_rule, model, kind,
              COUNT(*), SUM(out_est), SUM(1 - priced), SUM(is_error),
              SUM(inp), SUM(out), SUM(out_raw), SUM(COALESCE(think, 0)),
              SUM(CASE WHEN think IS NOT NULL THEN out ELSE 0 END),
              SUM(cr), SUM(w5), SUM(w1), SUM(ws), SUM(wf),
              SUM(cost), SUM(cost_raw), SUM(CASE WHEN out_est = 1 THEN cost ELSE 0 END),
              SUM(c_inp), SUM(c_out), SUM(c_cr), SUM(c_w5), SUM(c_w1), SUM(c_web),
              SUM(break_excess)
            FROM replies WHERE day >= ? GROUP BY day, project, project_rule, card, card_rule,
              model, kind""", (oldest,))
        con.commit()


def _bisect_right(a, x):
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if x < a[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def refresh(full: bool = False, min_interval: float = 0.0, blocking: bool = True) -> dict:
    ix = Index()
    try:
        return ix.refresh(full=full, min_interval=min_interval, blocking=blocking)
    finally:
        ix.close()
