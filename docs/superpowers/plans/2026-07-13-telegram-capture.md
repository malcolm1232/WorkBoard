# Telegram Idea Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user fire a link or idea at their own Telegram bot from their phone and have it appear as a claimable card on every WorkBoard, with no server and no cost.

**Architecture:** A stdlib-only poller, fired by launchd/systemd every 15 minutes, pulls messages via Telegram's `getUpdates` (outbound HTTPS only) and appends them to one shared inbox file at `~/.board-steward/inbox.jsonl`. Every board server renders a *virtual* "From Telegram" column from that shared file (the items are never in any board.json). Dragging an item into a real column atomically claims it: a real card is created on that board and the item disappears from every other board over SSE.

**Tech Stack:** Python 3 standard library only (`urllib`, `json`, `fcntl`, `plistlib`, `subprocess`). No new dependencies, no external services beyond Telegram's free bot API. Frontend is vanilla JS in `templates/board.html`. Tests are plain-python scripts in `dev/`.

**Spec:** `docs/superpowers/specs/2026-07-13-telegram-capture-design.md`
**Card:** WorkBoard #856

## Global Constraints

- **Stdlib only.** No new pip dependencies anywhere in this feature.
- **No server.** The poller makes outbound HTTPS calls only. Nothing new listens on a port. The only servers are the board servers that already run.
- **The poller never writes board.json.** Its only board-write path is invoking `card.py claim`, which routes through the existing sanctioned save path.
- **Lock ordering: board lock, then inbox lock. Never the reverse.** No code may acquire a board lock while holding the inbox lock, or the two will deadlock.
- **The virtual column and its items must never enter `state.cards` or `state.columns` in the browser.** `save()` POSTs the whole `state` object, so anything placed there would be persisted into board.json.
- **Style:** no em dashes in code, comments, docs, or commit messages (use a plain `-`). Match existing file conventions: `from __future__ import annotations`, 4-space indent, module-level constants in caps.
- **Test convention:** plain-python scripts in `dev/`, run as `python3 dev/test_856_*.py`, exit 0 = pass. No pytest. Use the `check(cond, msg)` + `_fails` counter pattern from `dev/test_841_boards_endpoint.py`.
- **Isolation in tests:** set `BOARD_INBOX`, `BOARD_TELEGRAM_CONFIG`, `BOARD_ASSIGNMENTS`, `BOARD_REGISTRY` env vars to temp paths *before* importing the modules under test. Tests must never touch the real inbox, the real config, or the live board.
- **Naming (locked across tasks):** inbox item ids are `T-<seq>` (e.g. `T-7`). The virtual column id is `__inbox__`. The card tag is `from-telegram`. The SSE event is `inbox-updated`.

## File Structure

| File | Responsibility |
| --- | --- |
| `scripts/_inbox.py` (new) | The shared inbox: read, append, reserve, finalize, release, discard. Flock-guarded. The only module that touches `inbox.jsonl`. Also `--hook-line` for the session digest. |
| `scripts/_tg_config.py` (new) | Telegram config load/save (token, chat_id, offset, custom aliases) plus per-user alias resolution against the board registry. No network. |
| `scripts/telegram_poller.py` (new) | `getUpdates` -> filter to owner -> append to inbox -> execute route hints via `card.py claim` -> confirm on Telegram -> advance offset. The only file that knows Telegram exists. |
| `scripts/card_state.py` (modify) | Gains `unique_card_id()` and `build_card()`: the single card-dict constructor, shared by `cmd_add` and the claim paths. |
| `scripts/card_commands.py` (modify) | Gains `cmd_inbox`, `cmd_claim`, `cmd_telegram_setup`, `cmd_telegram_alias`. `cmd_add` refactored onto `build_card`. |
| `scripts/card.py` (modify) | Registers the four new subparsers; `telegram-setup` is board-less like `board-new`. |
| `scripts/serve.py` (modify) | `GET /inbox`, `POST /inbox/claim`, `POST /inbox/discard`, `POST /inbox/notify`; broadcasts `inbox-updated`. |
| `templates/board.html` (modify) | Renders the virtual column, drag-to-claim, discard, toast, SSE + focus refresh. |
| `templates/board.json` (modify) | Adds `from-telegram` to the tag taxonomy so new boards style it. |
| `scripts/install_launchd.py` / `install_systemd.py` (modify) | Add the 15-minute poller job (launchd `StartInterval`, systemd timer). |
| `scripts/hook_session_start.sh` (modify) | Digest line for unclaimed captures + backgrounded catch-up poll. |
| `dev/test_856_*.py` (new) | One test script per task. |

---

### Task 1: The shared inbox library

**Files:**
- Create: `scripts/_inbox.py`
- Test: `dev/test_856_inbox.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces (used by every later task):
  - `path() -> Path`
  - `append(text: str, update_id: int, ts: str | None = None) -> dict | None` (returns `None` if `update_id` already present; parses the leading `#alias` into `routeHint` and the stripped `title`)
  - `unclaimed() -> list[dict]` (includes items stuck in `reserved` for more than `STALE_RESERVE_S`)
  - `get(tid: str) -> dict | None`
  - `reserve(tid: str) -> dict` (atomic CAS `unclaimed -> reserved`; raises `InboxConflict` if already claimed or freshly reserved; the returned item carries a `reserveToken`)
  - `finalize(tid: str, board: str, card_num: int, token: str) -> dict` (`reserved -> claimed`; the token must match the one `reserve` minted)
  - `release(tid: str, token: str) -> dict` (`reserved -> unclaimed`, for rollback)
  - `discard(tid: str) -> dict`
  - `counts() -> dict` with keys `unclaimed: int`, `oldest_age_days: float | None`
  - `notify_boards() -> None` (best-effort `POST /inbox/notify` to every live board; never raises)
  - `class InboxConflict(Exception)` with attribute `.claim: dict | None`
- Item schema (one JSON object per line):
  `{"tid": "T-7", "update_id": 812345678, "text": "#qm https://x.com/... look at this", "title": "https://x.com/... look at this", "url": "https://x.com/...", "routeHint": "qm", "ts": "2026-07-13T09:12:04Z", "status": "unclaimed", "claim": null}`
  where `status` is one of `unclaimed | reserved | claimed | discarded` and `claim` is `{"board": "/path/to/board", "cardNum": 431, "ts": "..."}` once claimed.

- [ ] **Step 1: Write the failing test**

Create `dev/test_856_inbox.py`:

```python
#!/usr/bin/env python3
"""#856 - shared capture inbox.

Run: python3 dev/test_856_inbox.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856inbox-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")

import _inbox  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def reset():
    p = _inbox.path()
    if p.exists():
        p.unlink()


def test_append_and_parse():
    print("append + parse")
    reset()
    it = _inbox.append("look at this https://ex.com/a", update_id=101)
    check(it["tid"] == "T-1", "first item is T-1")
    check(it["status"] == "unclaimed", "starts unclaimed")
    check(it["routeHint"] is None, "no hint when no prefix")
    check(it["url"] == "https://ex.com/a", "url extracted")
    check(it["title"] == "look at this https://ex.com/a", "title is the full text")

    it2 = _inbox.append("#qm https://ex.com/b", update_id=102)
    check(it2["tid"] == "T-2", "second item is T-2")
    check(it2["routeHint"] == "qm", "leading #alias parsed as routeHint")
    check(it2["title"] == "https://ex.com/b", "hint stripped from title")
    check(it2["text"] == "#qm https://ex.com/b", "text stays verbatim")


def test_dedupe():
    print("dedupe by update_id")
    reset()
    _inbox.append("a", update_id=200)
    dup = _inbox.append("a again", update_id=200)
    check(dup is None, "same update_id returns None")
    check(len(_inbox.unclaimed()) == 1, "only one item stored")


def test_claim_lifecycle():
    print("reserve / finalize / release / discard")
    reset()
    a = _inbox.append("a", update_id=300)
    _inbox.reserve(a["tid"])
    check(_inbox.get(a["tid"])["status"] == "reserved", "reserve flips to reserved")
    _inbox.finalize(a["tid"], board="/b/board", card_num=431)
    got = _inbox.get(a["tid"])
    check(got["status"] == "claimed", "finalize flips to claimed")
    check(got["claim"]["cardNum"] == 431, "claim records card num")
    check(_inbox.unclaimed() == [], "claimed item leaves the unclaimed list")

    b = _inbox.append("b", update_id=301)
    _inbox.reserve(b["tid"])
    _inbox.release(b["tid"])
    check(_inbox.get(b["tid"])["status"] == "unclaimed", "release restores unclaimed")

    c = _inbox.append("c", update_id=302)
    _inbox.discard(c["tid"])
    check(_inbox.get(c["tid"])["status"] == "discarded", "discard flips to discarded")
    check(len(_inbox.unclaimed()) == 1, "only the released item is unclaimed")


def test_first_wins_under_concurrency():
    print("atomic first-wins reserve")
    reset()
    it = _inbox.append("contested", update_id=400)
    winners, conflicts = [], []

    def go():
        try:
            _inbox.reserve(it["tid"])
            winners.append(1)
        except _inbox.InboxConflict:
            conflicts.append(1)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(len(winners) == 1, f"exactly one reserve wins (got {len(winners)})")
    check(len(conflicts) == 7, f"the other seven conflict (got {len(conflicts)})")


def test_conflict_carries_claim():
    print("conflict reports the winning claim")
    reset()
    it = _inbox.append("x", update_id=500)
    _inbox.reserve(it["tid"])
    _inbox.finalize(it["tid"], board="/qm/board", card_num=77)
    try:
        _inbox.reserve(it["tid"])
        check(False, "second reserve must raise")
    except _inbox.InboxConflict as e:
        check(e.claim["cardNum"] == 77, "InboxConflict carries the winning claim")


def test_corrupt_line_skipped():
    print("corrupt line tolerated")
    reset()
    _inbox.append("good", update_id=600)
    with _inbox.path().open("a") as fh:
        fh.write("{not json\n")
    _inbox.append("also good", update_id=601)
    check(len(_inbox.unclaimed()) == 2, "corrupt line skipped, real items survive")


def test_counts():
    print("counts for the session digest")
    reset()
    check(_inbox.counts()["unclaimed"] == 0, "empty inbox counts zero")
    _inbox.append("a", update_id=700)
    _inbox.append("b", update_id=701)
    c = _inbox.counts()
    check(c["unclaimed"] == 2, "counts unclaimed items")
    check(c["oldest_age_days"] is not None, "reports the oldest age")


if __name__ == "__main__":
    test_append_and_parse()
    test_dedupe()
    test_claim_lifecycle()
    test_first_wins_under_concurrency()
    test_conflict_carries_claim()
    test_corrupt_line_skipped()
    test_counts()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev/test_856_inbox.py`
Expected: FAIL with `ModuleNotFoundError: No module named '_inbox'`

- [ ] **Step 3: Write the implementation**

Create `scripts/_inbox.py`:

```python
"""Shared capture inbox for board-steward (#856).

One JSONL file, shared by every board, holding ideas captured from the phone.
Items are NOT board cards: they render as a virtual "From Telegram" column on
every board until someone claims one, at which point it becomes a real card on
exactly one board.

The file is the single source of truth. This module is the only code that
touches it. All mutations happen under an flock on a sidecar lock file.

LOCK ORDER: board lock first, then inbox lock. Never the reverse.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

INBOX_ENV = "BOARD_INBOX"
DEFAULT_PATH = Path.home() / ".board-steward" / "inbox.jsonl"

# A reserve that never finalized (crash between the two) frees itself after this.
STALE_RESERVE_S = 120

_URL_RE = re.compile(r"https?://\S+")
_HINT_RE = re.compile(r"^#([A-Za-z0-9_-]{1,32})\s+(.+)$", re.S)


class InboxConflict(Exception):
    """Raised when an item is already claimed (or freshly reserved) by someone else."""

    def __init__(self, msg: str, claim: dict | None = None):
        super().__init__(msg)
        self.claim = claim


def path() -> Path:
    env = os.environ.get(INBOX_ENV)
    return Path(env) if env else DEFAULT_PATH


def _lock_path() -> Path:
    return path().with_suffix(path().suffix + ".lock")


@contextmanager
def _locked(timeout: float = 5.0):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lp = _lock_path()
    deadline = time.time() + timeout
    fh = lp.open("a+")
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(f"inbox lock busy: {lp}")
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> list[dict]:
    """All items, corrupt lines skipped."""
    p = path()
    if not p.exists():
        return []
    items: list[dict] = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue  # corrupt line: skip, keep the rest
        if isinstance(obj, dict) and obj.get("tid"):
            items.append(obj)
    return items


def _write(items: list[dict]) -> None:
    """Atomic full rewrite. Callers MUST hold the lock."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(i, sort_keys=True) + "\n" for i in items))
    os.replace(tmp, p)


def _is_free(item: dict) -> bool:
    """Unclaimed, or a reserve that went stale (its claimer died)."""
    if item.get("status") == "unclaimed":
        return True
    if item.get("status") == "reserved":
        try:
            held = time.time() - float(item.get("reservedAt", 0))
        except (TypeError, ValueError):
            return True
        return held > STALE_RESERVE_S
    return False


def parse(text: str) -> tuple[str, str | None, str]:
    """Split a raw message into (title, routeHint, url)."""
    hint = None
    title = text.strip()
    m = _HINT_RE.match(title)
    if m:
        hint = m.group(1).lower()
        title = m.group(2).strip()
    url_m = _URL_RE.search(title)
    return title, hint, (url_m.group(0) if url_m else "")


def append(text: str, update_id: int, ts: str | None = None) -> dict | None:
    """Add a captured message. Returns None if update_id was already captured."""
    with _locked():
        items = _read()
        if any(i.get("update_id") == update_id for i in items):
            return None  # at-least-once delivery: a redelivered update is a no-op
        title, hint, url = parse(text)
        item = {
            "tid": f"T-{len(items) + 1}",
            "update_id": update_id,
            "text": text,
            "title": title,
            "url": url,
            "routeHint": hint,
            "ts": ts or _now(),
            "status": "unclaimed",
            "claim": None,
        }
        items.append(item)
        _write(items)
        return item


def get(tid: str) -> dict | None:
    for i in _read():
        if i["tid"] == tid:
            return i
    return None


def unclaimed() -> list[dict]:
    return [i for i in _read() if _is_free(i)]


def counts() -> dict:
    free = unclaimed()
    oldest = None
    for i in free:
        try:
            t = datetime.strptime(i["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        age = (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
        oldest = age if oldest is None else max(oldest, age)
    return {"unclaimed": len(free), "oldest_age_days": oldest}


def _mutate(tid: str, fn) -> dict:
    with _locked():
        items = _read()
        for idx, item in enumerate(items):
            if item["tid"] == tid:
                items[idx] = fn(item)
                _write(items)
                return items[idx]
        raise KeyError(f"no inbox item {tid}")


def reserve(tid: str) -> dict:
    """Atomic CAS: unclaimed -> reserved. First caller wins; the rest raise."""
    with _locked():
        items = _read()
        for idx, item in enumerate(items):
            if item["tid"] != tid:
                continue
            if not _is_free(item):
                raise InboxConflict(
                    f"{tid} is already {item.get('status')}", item.get("claim")
                )
            item = dict(item, status="reserved", reservedAt=time.time())
            items[idx] = item
            _write(items)
            return item
        raise KeyError(f"no inbox item {tid}")


def finalize(tid: str, board: str, card_num: int) -> dict:
    return _mutate(
        tid,
        lambda i: dict(
            i,
            status="claimed",
            reservedAt=None,
            claim={"board": board, "cardNum": card_num, "ts": _now()},
        ),
    )


def release(tid: str) -> dict:
    return _mutate(tid, lambda i: dict(i, status="unclaimed", reservedAt=None))


def discard(tid: str) -> dict:
    return _mutate(tid, lambda i: dict(i, status="discarded", reservedAt=None))


def notify_boards() -> None:
    """Best-effort: tell every live board server the inbox changed. Never raises."""
    try:
        import port_registry
    except Exception:
        return
    token = os.environ.get("BOARD_AUTH_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        live = port_registry.read()
    except Exception:
        return
    for entry in live.values():
        port = entry.get("port")
        if not port:
            continue
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/inbox/notify",
                data=b"{}",
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1).read()
        except Exception:
            continue  # board down or busy: it will pick the change up on next fetch


def _hook_line() -> str:
    c = counts()
    if not c["unclaimed"]:
        return ""
    age = c["oldest_age_days"] or 0
    oldest = f", oldest {age:.0f}d" if age >= 1 else ""
    return (
        f"CAPTURES: {c['unclaimed']} unclaimed from Telegram{oldest} - they sit in the "
        f"From Telegram column on every board; claim with `card.py claim <T-n>` or by dragging."
    )


if __name__ == "__main__":
    import sys

    if "--hook-line" in sys.argv:
        line = _hook_line()
        if line:
            print(line)
    else:
        for it in unclaimed():
            print(f"{it['tid']}  {it['title'][:70]}")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 dev/test_856_inbox.py`
Expected: `PASS`, exit 0

- [ ] **Step 5: Commit**

```bash
git add scripts/_inbox.py dev/test_856_inbox.py
git commit -m "feat(#856): shared capture inbox with atomic first-wins claim"
```

---

### Task 2: Shared card constructor

The claim paths (server and CLI) must build cards with exactly the schema `cmd_add` produces. Rather than duplicating the dict, extract it once and refactor `cmd_add` onto it.

**Files:**
- Modify: `scripts/card_state.py` (append two functions at the end of the module)
- Modify: `scripts/card_commands.py:40-158` (`cmd_add` uses the new helpers)
- Test: `dev/test_856_build_card.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `card_state.unique_card_id(d: dict, base: str) -> str` (appends `-2`, `-3`, ... until unused)
  - `card_state.build_card(d, *, title, column, cid=None, code="", priority="medium", tags=None, origin="", notes="", writeup="", created=None, meta=None) -> dict`
    Assigns `num = d["nextNum"]`, appends to `d["cards"]`, bumps `d["nextNum"]`, returns the card. Does NOT save and does NOT validate tags (tag policy stays in `cmd_add`).

- [ ] **Step 1: Write the failing test**

Create `dev/test_856_build_card.py`:

```python
#!/usr/bin/env python3
"""#856 - shared card constructor.

Run: python3 dev/test_856_build_card.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import card_state  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def board():
    return {
        "rev": 1,
        "nextNum": 10,
        "columns": [{"id": "task", "name": "Task"}, {"id": "done", "name": "Done"}],
        "cards": [],
    }


def test_schema():
    print("card schema")
    d = board()
    c = card_state.build_card(d, title="hello", column="task")
    for k in (
        "num", "id", "code", "priority", "title", "column", "tags", "origin",
        "notes", "writeup", "createdAt", "updatedAt", "doneAt",
        "lastTouchedSubtask", "linkedCards", "subtasks",
    ):
        check(k in c, f"card has {k}")
    check(c["num"] == 10, "num taken from nextNum")
    check(d["nextNum"] == 11, "nextNum bumped")
    check(d["cards"][0] is c, "card appended to the board")
    check(c["doneAt"] is None, "doneAt is None for a non-done column")
    check(c["id"] == "c-hello", "id slugified from the title")


def test_done_column_stamps_doneat():
    print("done column")
    d = board()
    c = card_state.build_card(d, title="shipped", column="done")
    check(c["doneAt"] == c["createdAt"], "doneAt stamped when created in done")


def test_unique_id():
    print("unique ids")
    d = board()
    a = card_state.build_card(d, title="same", column="task")
    b = card_state.build_card(d, title="same", column="task")
    check(a["id"] == "c-same", "first id is the plain slug")
    check(b["id"] == "c-same-2", "second id is suffixed")
    check(card_state.unique_card_id(d, "c-same") == "c-same-3", "unique_card_id skips taken ids")


def test_meta_and_tags_passthrough():
    print("meta + tags")
    d = board()
    c = card_state.build_card(
        d, title="x", column="task", tags=["from-telegram"],
        origin="#qm x", meta={"telegram": {"tid": "T-3"}},
    )
    check(c["tags"] == ["from-telegram"], "tags passed through unvalidated")
    check(c["origin"] == "#qm x", "origin is the verbatim message")
    check(c["meta"]["telegram"]["tid"] == "T-3", "meta passed through")


def test_cmd_add_still_matches():
    """cmd_add must keep producing the same shape after the refactor."""
    print("cmd_add regression")
    import argparse

    import card_commands

    d = board()
    args = argparse.Namespace(
        title="a task", column="task", code=None, id=None, priority="medium",
        tag=[], origin=None, origin_stdin=False, notes=None, notes_stdin=False,
        writeup=None, writeup_stdin=False, created_at=None, force=True, auto=False,
        link=None, pause_ms=None,
    )
    saved = {}
    card_state_ref = card_commands.atomic_save

    def fake_save(p, dd, regen=True):
        saved["d"] = dd
        return 2

    card_commands.atomic_save = fake_save
    try:
        card_commands.cmd_add(args, d, Path("/tmp/nope/board.json"))
    finally:
        card_commands.atomic_save = card_state_ref

    c = saved["d"]["cards"][0]
    check(c["num"] == 10, "cmd_add still assigns from nextNum")
    check(c["title"] == "a task", "cmd_add still sets the title")
    check(c["column"] == "task", "cmd_add still sets the column")
    check(saved["d"]["nextNum"] == 11, "cmd_add still bumps nextNum")
    check("subtasks" in c and "linkedCards" in c, "cmd_add still emits the full schema")


if __name__ == "__main__":
    test_schema()
    test_done_column_stamps_doneat()
    test_unique_id()
    test_meta_and_tags_passthrough()
    test_cmd_add_still_matches()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

Note on `test_cmd_add_still_matches`: it fakes an argparse Namespace. If `cmd_add` reads a field this Namespace does not carry, add the field to the Namespace. Do not change `cmd_add` to suit the test: the point of this test is that `cmd_add`'s output is unchanged by the refactor.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev/test_856_build_card.py`
Expected: FAIL with `AttributeError: module 'card_state' has no attribute 'build_card'`

- [ ] **Step 3: Add the helpers to `card_state.py`**

Append to the end of `scripts/card_state.py`:

```python
def unique_card_id(d: dict, base: str) -> str:
    """Return `base`, or `base-2`, `base-3`, ... if it is already taken."""
    taken = {c.get("id") for c in d.get("cards", [])}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def build_card(
    d: dict,
    *,
    title: str,
    column: str,
    cid: str | None = None,
    code: str = "",
    priority: str = "medium",
    tags: list[str] | None = None,
    origin: str = "",
    notes: str = "",
    writeup: str = "",
    created: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Construct a card, append it to the board dict, bump nextNum, return it.

    The single source of truth for the card schema. Does not save and does not
    validate tags: tag policy belongs to the caller (see cmd_add._check_tags).
    """
    now = now_iso()
    created = created or now
    cid = unique_card_id(d, cid or f"c-{slugify(code or title)}")
    card = {
        "num": d["nextNum"],
        "id": cid,
        "code": code or "",
        "priority": priority,
        "title": title,
        "column": column,
        "tags": list(tags or []),
        "origin": origin,
        "notes": notes,
        "writeup": writeup,
        "createdAt": created,
        "updatedAt": now,
        "doneAt": created if column == "done" else None,
        "lastTouchedSubtask": None,
        "linkedCards": [],
        "subtasks": [],
    }
    if meta:
        card["meta"] = meta
    d["cards"].append(card)
    d["nextNum"] += 1
    return card
```

- [ ] **Step 4: Refactor `cmd_add` onto the helpers**

In `scripts/card_commands.py`, replace the id-dedupe block (currently around lines 41-53) with a single call, and the card-dict literal (currently around lines 106-130) with a `build_card` call. Keep every existing policy step (`_check_tags`, `_detect_urgency`, `--auto`, the `auto_card` meta, `_set_active_work`, `_record_move`, `atomic_save`) exactly as it is. The card construction becomes:

```python
    card = build_card(
        d,
        cid=cid,
        title=args.title,
        column=target_col,
        code=args.code or "",
        priority=target_prio,
        tags=tags,
        origin=origin,
        notes=notes,
        writeup=writeup,
        created=created,
        meta=({"autoCreated": True, "autoSource": auto_source} if auto_card else None),
    )
```

and the old `d["cards"].append(card)` / `d["nextNum"] += 1` lines are deleted (`build_card` does both). Import the helpers at the top of `card_commands.py` alongside the existing `card_state` imports:

```python
from card_state import build_card, unique_card_id  # noqa: F401
```

Use `unique_card_id(d, base)` where `cid` was previously de-duped by hand.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 dev/test_856_build_card.py`
Expected: `PASS`

Run the existing suite to prove no regression:
`for t in dev/test_*.py; do python3 "$t" >/dev/null 2>&1 || echo "FAILED: $t"; done`
Expected: no `FAILED:` lines (or the same set that already failed on `main` before this task; check with `git stash` if unsure).

- [ ] **Step 6: Commit**

```bash
git add scripts/card_state.py scripts/card_commands.py dev/test_856_build_card.py
git commit -m "refactor(#856): single card constructor shared by add and claim"
```

---

### Task 3: Telegram config and per-user alias resolution

**Files:**
- Create: `scripts/_tg_config.py`
- Test: `dev/test_856_tg_config.py`

**Interfaces:**
- Consumes: `port_registry.assignments()` (existing, returns `{board_dir: port}`).
- Produces:
  - `config_path() -> Path` (env `BOARD_TELEGRAM_CONFIG`, default `~/.board-steward/telegram.json`)
  - `load() -> dict | None` (None when not configured)
  - `save(conf: dict) -> None` (writes with mode 600)
  - `set_offset(offset: int) -> None`
  - `set_status(status: str | None) -> None` (surfaces poller health in the digest)
  - `aliases() -> dict[str, str]` (alias -> board dir; custom aliases override derived ones)
  - `resolve_board(alias: str | None) -> Path | None` (exact, then unique-prefix; ambiguous or unknown returns None)
  - `task_column(d: dict) -> str` (the board's `task` column id, else the first task-like column, else the first column)
- Config schema: `{"token": "...", "chat_id": 12345, "offset": 0, "aliases": {"qm": "/Users/x/Desktop/QuantifyMe/HFTAgents/board"}, "status": null}`

- [ ] **Step 1: Write the failing test**

Create `dev/test_856_tg_config.py`:

```python
#!/usr/bin/env python3
"""#856 - telegram config + per-user alias resolution.

Run: python3 dev/test_856_tg_config.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856cfg-"))
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")

import _tg_config as cfg  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def write_assignments(mapping):
    Path(os.environ["BOARD_ASSIGNMENTS"]).write_text(json.dumps(mapping))


def test_load_save_perms():
    print("config load/save")
    check(cfg.load() is None, "unconfigured returns None")
    cfg.save({"token": "T", "chat_id": 42, "offset": 0})
    conf = cfg.load()
    check(conf["token"] == "T" and conf["chat_id"] == 42, "round-trips")
    mode = stat.S_IMODE(cfg.config_path().stat().st_mode)
    check(mode == 0o600, f"config is chmod 600 (got {oct(mode)})")

    cfg.set_offset(99)
    check(cfg.load()["offset"] == 99, "offset persisted")
    cfg.set_status("token invalid")
    check(cfg.load()["status"] == "token invalid", "status persisted")
    cfg.set_status(None)
    check(cfg.load()["status"] is None, "status cleared")


def test_derived_aliases():
    print("aliases derived from the user's own boards")
    write_assignments({
        "/Users/x/Desktop/WorkBoard/board": 7891,
        "/Users/x/Desktop/QuantifyMe/HFTAgents/board": 7893,
        "/Users/x/Desktop/TradingResearch/board": 7895,
    })
    cfg.save({"token": "T", "chat_id": 42, "offset": 0})
    al = cfg.aliases()
    check(al.get("workboard", "").endswith("/WorkBoard/board"), "folder name becomes an alias")
    check(al.get("hftagents", "").endswith("/HFTAgents/board"), "nested project folder used")
    check("qm" not in al, "no hardcoded personal alias")

    check(cfg.resolve_board("workboard").name == "board", "exact alias resolves")
    check(cfg.resolve_board("trading").name == "board", "unique prefix resolves")
    check(cfg.resolve_board("nope") is None, "unknown alias resolves to None")
    check(cfg.resolve_board(None) is None, "no hint resolves to None")


def test_custom_alias_wins():
    print("custom aliases")
    write_assignments({
        "/Users/x/Desktop/WorkBoard/board": 7891,
        "/Users/x/Desktop/QuantifyMe/HFTAgents/board": 7893,
    })
    cfg.save({
        "token": "T", "chat_id": 42, "offset": 0,
        "aliases": {"qm": "/Users/x/Desktop/QuantifyMe/HFTAgents/board",
                    "workboard": "/Users/x/Desktop/QuantifyMe/HFTAgents/board"},
    })
    check(str(cfg.resolve_board("qm")).endswith("/HFTAgents/board"), "custom alias resolves")
    check(str(cfg.resolve_board("workboard")).endswith("/HFTAgents/board"),
          "custom alias overrides the derived one")


def test_ambiguous_alias_is_none():
    print("ambiguity never guesses")
    write_assignments({
        "/Users/x/a/Kaggle/board": 7897,
        "/Users/x/b/Kaggle/board": 7898,
    })
    cfg.save({"token": "T", "chat_id": 42, "offset": 0})
    check(cfg.resolve_board("kaggle") is None, "duplicate folder names resolve to None")

    write_assignments({
        "/Users/x/Desktop/Trade/board": 7895,
        "/Users/x/Desktop/Trading/board": 7896,
    })
    check(cfg.resolve_board("trad") is None, "ambiguous prefix resolves to None")
    check(str(cfg.resolve_board("trading")).endswith("/Trading/board"), "exact still wins over prefix")


def test_task_column():
    print("task column pick")
    check(cfg.task_column({"columns": [{"id": "notes"}, {"id": "task"}]}) == "task",
          "prefers the task column")
    check(cfg.task_column({"columns": [{"id": "c-1", "name": "Inbox Tasks"}, {"id": "done"}]}) == "c-1",
          "falls back to a task-like name")
    check(cfg.task_column({"columns": [{"id": "notes"}, {"id": "done"}]}) == "notes",
          "falls back to the first column")


if __name__ == "__main__":
    test_load_save_perms()
    test_derived_aliases()
    test_custom_alias_wins()
    test_ambiguous_alias_is_none()
    test_task_column()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev/test_856_tg_config.py`
Expected: FAIL with `ModuleNotFoundError: No module named '_tg_config'`

- [ ] **Step 3: Write the implementation**

Create `scripts/_tg_config.py`:

```python
"""Telegram capture config + per-user alias resolution (#856).

Aliases are NEVER hardcoded. They are derived from the user's own board
registry (the project folder name of each board they run), plus any custom
short aliases they set. An alias that is unknown or ambiguous resolves to
None: we do not guess which board an idea belongs to.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

CONFIG_ENV = "BOARD_TELEGRAM_CONFIG"
DEFAULT_PATH = Path.home() / ".board-steward" / "telegram.json"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    return Path(env) if env else DEFAULT_PATH


def load() -> dict | None:
    p = config_path()
    if not p.exists():
        return None
    try:
        conf = json.loads(p.read_text())
    except Exception:
        return None
    if not conf.get("token") or not conf.get("chat_id"):
        return None
    return conf


def save(conf: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)  # the token is a credential: never group/world readable
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def _patch(**fields) -> None:
    conf = load() or {}
    conf.update(fields)
    save(conf)


def set_offset(offset: int) -> None:
    _patch(offset=int(offset))


def set_status(status: str | None) -> None:
    _patch(status=status)


def _slug(s: str) -> str:
    return _SLUG_RE.sub("", s.lower())


def _board_dirs() -> list[str]:
    try:
        import port_registry

        return list(port_registry.assignments().keys())
    except Exception:
        return []


def aliases() -> dict[str, str]:
    """alias -> board dir. Derived from the user's boards; custom aliases win.

    A folder name shared by two boards is dropped: it is ambiguous, so it must
    not resolve at all.
    """
    derived: dict[str, list[str]] = {}
    for d in _board_dirs():
        name = Path(d).parent.name  # ".../QuantifyMe/HFTAgents/board" -> "HFTAgents"
        derived.setdefault(_slug(name), []).append(d)
    out = {a: paths[0] for a, paths in derived.items() if len(paths) == 1}
    conf = load() or {}
    for alias, d in (conf.get("aliases") or {}).items():
        out[_slug(alias)] = d
    return out


def resolve_board(alias: str | None) -> Path | None:
    """Exact match, then unique prefix. Ambiguous or unknown returns None."""
    if not alias:
        return None
    a = _slug(alias)
    al = aliases()
    if a in al:
        return Path(al[a])
    hits = [d for name, d in al.items() if name.startswith(a)]
    if len(hits) == 1:
        return Path(hits[0])
    return None  # zero hits or ambiguous: never guess


def task_column(d: dict) -> str:
    """Where a routed capture lands: the task column, else task-like, else first."""
    cols = d.get("columns") or []
    for c in cols:
        if c.get("id") == "task":
            return "task"
    for c in cols:
        blob = f"{c.get('id', '')} {c.get('name', '')}".lower()
        if "task" in blob or "inbox" in blob or "todo" in blob:
            return c["id"]
    return cols[0]["id"] if cols else "task"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 dev/test_856_tg_config.py`
Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add scripts/_tg_config.py dev/test_856_tg_config.py
git commit -m "feat(#856): telegram config + per-user board alias resolution"
```

---

### Task 4: The poller

**Files:**
- Create: `scripts/telegram_poller.py`
- Test: `dev/test_856_poller.py`

**Interfaces:**
- Consumes: `_inbox.append/notify_boards`, `_tg_config.load/set_offset/set_status/resolve_board`.
- Produces:
  - `poll(api=None, timeout: float = 10.0) -> list[dict]` (the newly captured items; `api` is injectable for tests)
  - `_api(token: str, method: str, params: dict, timeout: float) -> dict` (the real Telegram transport)
- Behavioural contract (each is a test):
  - Messages from any chat other than the configured `chat_id` are ignored.
  - A redelivered `update_id` is not double-captured.
  - The offset advances **only after** the inbox write lands. If the inbox write raises, the offset must not move.
  - A network failure is a silent no-op that leaves the offset untouched.
  - A 401 sets `status` in the config so the session digest can surface it.
  - Every captured item gets exactly one confirmation reply. A resolved route hint claims the card and the reply echoes `saved -> <alias> #<num>`.

- [ ] **Step 1: Write the failing test**

Create `dev/test_856_poller.py`:

```python
#!/usr/bin/env python3
"""#856 - telegram poller.

Run: python3 dev/test_856_poller.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856poll-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")

import _inbox  # noqa: E402
import _tg_config as cfg  # noqa: E402
import telegram_poller as tp  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def reset(offset=0):
    p = _inbox.path()
    if p.exists():
        p.unlink()
    cfg.save({"token": "TOK", "chat_id": 42, "offset": offset})


def fake_api(updates, sent=None, fail=None):
    """A stand-in Telegram API. Records sendMessage calls into `sent`."""

    def api(token, method, params, timeout):
        if fail:
            raise fail
        if method == "getUpdates":
            off = int(params.get("offset", 0))
            return {"ok": True, "result": [u for u in updates if u["update_id"] >= off]}
        if method == "sendMessage":
            if sent is not None:
                sent.append(params["text"])
            return {"ok": True}
        raise AssertionError(f"unexpected method {method}")

    return api


def upd(uid, text, chat=42):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_captures_and_confirms():
    print("capture + confirm")
    reset()
    sent = []
    new = tp.poll(api=fake_api([upd(10, "https://ex.com/a")], sent))
    check(len(new) == 1, "one item captured")
    check(new[0]["title"] == "https://ex.com/a", "title stored")
    check(len(sent) == 1 and "saved" in sent[0].lower(), f"one confirmation sent ({sent})")
    check(cfg.load()["offset"] == 11, "offset advanced past the update")


def test_ignores_other_chats():
    print("chat_id allowlist")
    reset()
    new = tp.poll(api=fake_api([upd(20, "stranger", chat=999)]))
    check(new == [], "message from another chat ignored")
    check(_inbox.unclaimed() == [], "nothing captured")
    check(cfg.load()["offset"] == 21, "offset still advances past the ignored update")


def test_ignores_commands():
    print("bot commands")
    reset()
    new = tp.poll(api=fake_api([upd(30, "/start")]))
    check(new == [], "/start is not captured as an idea")


def test_dedupe_on_redelivery():
    print("redelivery")
    reset()
    updates = [upd(40, "once")]
    tp.poll(api=fake_api(updates))
    cfg.set_offset(40)  # simulate a crash before the offset advanced
    new = tp.poll(api=fake_api(updates))
    check(new == [], "redelivered update is not captured twice")
    check(len(_inbox.unclaimed()) == 1, "still exactly one item")


def test_network_failure_is_a_noop():
    print("network down")
    reset(offset=5)
    new = tp.poll(api=fake_api([], fail=urllib.error.URLError("down")))
    check(new == [], "no items")
    check(cfg.load()["offset"] == 5, "offset untouched so the next tick retries")


def test_bad_token_sets_status():
    print("invalid token")
    reset()
    err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
    tp.poll(api=fake_api([], fail=err))
    check("token" in (cfg.load().get("status") or "").lower(),
          "401 recorded in config status for the session digest")


def test_offset_not_advanced_if_inbox_write_fails():
    print("offset ordering")
    reset(offset=1)
    real_append = _inbox.append

    def boom(*a, **k):
        raise OSError("disk full")

    _inbox.append = boom
    try:
        try:
            tp.poll(api=fake_api([upd(50, "x")]))
        except OSError:
            pass
    finally:
        _inbox.append = real_append
    check(cfg.load()["offset"] == 1, "offset must not advance when the inbox write fails")


def test_route_hint_claims_and_echoes():
    print("route hint")
    reset()
    sent = []
    claimed = {}

    def fake_claim(item):
        claimed["tid"] = item["tid"]
        return "saved -> qm #431"

    real = tp._claim_hint
    tp._claim_hint = fake_claim
    try:
        tp.poll(api=fake_api([upd(60, "#qm https://ex.com/z")], sent))
    finally:
        tp._claim_hint = real
    check(claimed.get("tid") is not None, "hinted item routed to the claim path")
    check(sent and sent[0] == "saved -> qm #431", f"reply echoes the claim ({sent})")


if __name__ == "__main__":
    test_captures_and_confirms()
    test_ignores_other_chats()
    test_ignores_commands()
    test_dedupe_on_redelivery()
    test_network_failure_is_a_noop()
    test_bad_token_sets_status()
    test_offset_not_advanced_if_inbox_write_fails()
    test_route_hint_claims_and_echoes()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev/test_856_poller.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'telegram_poller'`

- [ ] **Step 3: Write the implementation**

Create `scripts/telegram_poller.py`:

```python
#!/usr/bin/env python3
"""Telegram capture poller (#856).

Pulls messages from the user's own bot with getUpdates: outbound HTTPS only,
no webhook, no server, no cost. Fired every 15 minutes by launchd/systemd, and
once (backgrounded) at session start as a catch-up.

Delivery is at-least-once: the Telegram offset is advanced ONLY after the inbox
write has landed, and the inbox dedupes on update_id, so a crash or a retry can
never lose or double a capture.

This file is the only place that knows Telegram exists. Everything downstream
(inbox, virtual column, claiming) is channel agnostic.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _inbox  # noqa: E402
import _tg_config as cfg  # noqa: E402

API_URL = "https://api.telegram.org/bot{token}/{method}"
CARD_PY = Path(__file__).resolve().parent / "card.py"
_NUM_RE = re.compile(r"#(\d+)")


def _api(token: str, method: str, params: dict, timeout: float) -> dict:
    url = API_URL.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _claim_hint(item: dict) -> str | None:
    """Execute a resolved route hint by invoking card.py claim.

    card.py is the single sanctioned board-write path: it routes the save
    through the running board server, so the card flies in live. The poller
    never touches board.json itself.
    """
    board = cfg.resolve_board(item.get("routeHint"))
    if not board:
        return None  # unknown or ambiguous: leave it in the global column
    try:
        r = subprocess.run(
            [sys.executable, str(CARD_PY), "--board", str(Path(board) / "board.json"),
             "claim", item["tid"]],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    m = _NUM_RE.search(r.stdout)
    return f"saved -> {item['routeHint']} #{m.group(1)}" if m else "saved"


def poll(api=None, timeout: float = 10.0) -> list[dict]:
    """One capture cycle. Returns the newly captured items."""
    api = api or _api
    conf = cfg.load()
    if not conf:
        return []  # not configured: silent no-op
    token, chat_id = conf["token"], conf["chat_id"]
    offset = int(conf.get("offset") or 0)

    try:
        resp = api(token, "getUpdates", {"offset": offset, "timeout": 0}, timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            cfg.set_status("Telegram token invalid or revoked - re-run `card.py telegram-setup`")
        return []
    except Exception:
        return []  # network down: offset untouched, the next tick retries

    if not resp.get("ok"):
        return []
    if conf.get("status"):
        cfg.set_status(None)  # recovered

    new: list[dict] = []
    max_uid = offset - 1
    for u in resp.get("result", []):
        uid = u.get("update_id")
        if uid is None:
            continue
        max_uid = max(max_uid, uid)
        msg = u.get("message") or u.get("channel_post") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue  # allowlist: anyone can find a bot, only the owner may feed the board
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text or text.startswith("/"):
            continue  # /start and friends are not ideas
        item = _inbox.append(text=text, update_id=uid)
        if item:
            new.append(item)

    for item in new:
        reply = _claim_hint(item) or "saved"
        try:
            api(token, "sendMessage", {"chat_id": chat_id, "text": reply}, timeout)
        except Exception:
            pass  # the capture landed; a failed confirmation must not lose it

    if max_uid >= offset:
        cfg.set_offset(max_uid + 1)  # only now: every inbox write above has landed
    if new:
        _inbox.notify_boards()
    return new


def main() -> int:
    items = poll()
    for i in items:
        print(f"+ {i['tid']}  {i['title'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 dev/test_856_poller.py`
Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add scripts/telegram_poller.py dev/test_856_poller.py
git commit -m "feat(#856): telegram poller - pull captures, dedupe, route hints, confirm"
```

---

### Task 5: CLI subcommands (`inbox`, `claim`, `telegram-setup`, `telegram-alias`)

**Files:**
- Modify: `scripts/card_commands.py` (add four `cmd_*` functions)
- Modify: `scripts/card.py:387` (register subparsers, before `return ap`), `card.py:401-404` (`_READ_ONLY_CMDS`), `card.py:616-617` (board-less dispatch)
- Test: `dev/test_856_cli_claim.py`

**Interfaces:**
- Consumes: `_inbox` (Task 1), `card_state.build_card` (Task 2), `_tg_config` (Task 3).
- Produces:
  - `cmd_inbox(args, d, board)` - prints unclaimed items (read-only, must be added to `_READ_ONLY_CMDS`)
  - `cmd_claim(args, d, board)` - reserve, build card, save, finalize. On save failure it releases the reservation. Prints `+ #<num> <title> -> <col>  (rev N, from telegram T-n)`.
  - `cmd_telegram_setup(args)` - board-less, dispatched like `board-new`
  - `cmd_telegram_alias(args)` - board-less
- CLI: `card.py claim T-7 [--column notes]`; `--column` defaults to `_tg_config.task_column(d)`.

- [ ] **Step 1: Write the failing test**

Create `dev/test_856_cli_claim.py`:

```python
#!/usr/bin/env python3
"""#856 - card.py claim.

Run: python3 dev/test_856_cli_claim.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856cli-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")

import _inbox  # noqa: E402
import card_commands  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def mk_board():
    bd = Path(tempfile.mkdtemp(prefix="t856board-")) / "board"
    bd.mkdir(parents=True)
    d = {
        "title": "T", "rev": 1, "nextNum": 5, "schemaVersion": 3,
        "columns": [{"id": "notes", "name": "Notes"}, {"id": "task", "name": "Task"}],
        "cards": [], "activeWork": None,
    }
    (bd / "board.json").write_text(json.dumps(d))
    return bd / "board.json", d


def reset_inbox():
    p = _inbox.path()
    if p.exists():
        p.unlink()


def patched_save(saved):
    def fake(p, dd, regen=True):
        saved["d"] = dd
        Path(p).write_text(json.dumps(dd))
        return dd.get("rev", 1) + 1

    return fake


def test_claim_creates_card_and_marks_item():
    print("claim")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("#x look at this https://ex.com/a", update_id=900)

    saved = {}
    real = card_commands.atomic_save
    card_commands.atomic_save = patched_save(saved)
    try:
        args = argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"])
        card_commands.cmd_claim(args, d, board)
    finally:
        card_commands.atomic_save = real

    card = saved["d"]["cards"][0]
    check(card["title"] == "look at this https://ex.com/a", "title from the message, hint stripped")
    check(card["origin"] == "#x look at this https://ex.com/a", "origin is the verbatim message")
    check("from-telegram" in card["tags"], "tagged from-telegram")
    check(card["column"] == "task", "defaults to the task column")
    check(card["meta"]["telegram"]["tid"] == it["tid"], "meta records the inbox id")

    item = _inbox.get(it["tid"])
    check(item["status"] == "claimed", "inbox item claimed")
    check(item["claim"]["cardNum"] == card["num"], "claim records the card number")
    check(_inbox.unclaimed() == [], "item gone from the global column")


def test_claim_respects_explicit_column():
    print("explicit column")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("note this", update_id=901)
    saved = {}
    real = card_commands.atomic_save
    card_commands.atomic_save = patched_save(saved)
    try:
        card_commands.cmd_claim(
            argparse.Namespace(tid=it["tid"], column="notes", ref=it["tid"]), d, board
        )
    finally:
        card_commands.atomic_save = real
    check(saved["d"]["cards"][0]["column"] == "notes", "--column honoured")


def test_double_claim_rejected():
    print("double claim")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("once", update_id=902)
    saved = {}
    real = card_commands.atomic_save
    card_commands.atomic_save = patched_save(saved)
    try:
        card_commands.cmd_claim(argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board)
        try:
            card_commands.cmd_claim(argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board)
            check(False, "second claim must fail")
        except SystemExit:
            check(True, "second claim exits with an error")
    finally:
        card_commands.atomic_save = real


def test_failed_save_releases_reservation():
    print("rollback")
    reset_inbox()
    board, d = mk_board()
    it = _inbox.append("boom", update_id=903)

    real = card_commands.atomic_save

    def boom(p, dd, regen=True):
        raise RuntimeError("save failed")

    card_commands.atomic_save = boom
    try:
        try:
            card_commands.cmd_claim(argparse.Namespace(tid=it["tid"], column=None, ref=it["tid"]), d, board)
        except Exception:
            pass
    finally:
        card_commands.atomic_save = real

    check(_inbox.get(it["tid"])["status"] == "unclaimed",
          "a failed save releases the reservation so the item is claimable again")


if __name__ == "__main__":
    test_claim_creates_card_and_marks_item()
    test_claim_respects_explicit_column()
    test_double_claim_rejected()
    test_failed_save_releases_reservation()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev/test_856_cli_claim.py`
Expected: FAIL with `AttributeError: module 'card_commands' has no attribute 'cmd_claim'`

- [ ] **Step 3: Add the commands to `card_commands.py`**

Append to `scripts/card_commands.py`:

```python
def cmd_inbox(args, d, board):
    """List unclaimed phone captures (read-only)."""
    import _inbox

    items = _inbox.unclaimed()
    if not items:
        print("inbox empty - nothing captured from Telegram")
        return
    for i in items:
        hint = f"  [#{i['routeHint']}]" if i.get("routeHint") else ""
        print(f"{i['tid']:>6}  {i['ts']}  {i['title'][:60]}{hint}")
    print(f"\n{len(items)} unclaimed - claim with `card.py claim <T-n> [--column <col>]`")


def cmd_claim(args, d, board):
    """Promote an inbox capture to a real card on THIS board."""
    import _inbox
    import _tg_config as cfg

    tid = args.tid
    item = _inbox.get(tid)
    if not item:
        sys.exit(f"error: no inbox item {tid}")

    try:
        item = _inbox.reserve(tid)
    except _inbox.InboxConflict as e:
        claim = e.claim or {}
        where = f" -> {claim.get('board')} #{claim.get('cardNum')}" if claim else ""
        sys.exit(f"error: {tid} is already claimed{where}")

    token = item["reserveToken"]
    col = args.column or cfg.task_column(d)
    try:
        card = build_card(
            d,
            title=item["title"],
            column=col,
            tags=["from-telegram"],
            origin=item["text"],
            meta={"telegram": {"tid": tid, "updateId": item.get("update_id"),
                               "capturedAt": item.get("ts")}},
        )
        _set_active_work(d, card, "", col)
        _record_move(card, None, col)
        rev = atomic_save(board, d)
    except Exception:
        _inbox.release(tid, token)  # never strand a reservation
        raise

    _inbox.finalize(tid, board=str(Path(board).parent), card_num=card["num"], token=token)
    _inbox.notify_boards()
    print(f"+ #{card['num']} {card['title'][:50]} -> {col}  (rev {rev}, from telegram {tid})")


def cmd_telegram_setup(args):
    """One-time wizard: connect a Telegram bot to this machine."""
    import _tg_config as cfg
    import telegram_poller

    print("Telegram capture setup")
    print("  1. In Telegram, message @BotFather and send: /newbot")
    print("  2. Pick a name; BotFather replies with a token like 123456:ABC-DEF...")
    token = (args.token or input("  Paste the token: ")).strip()
    if not token or ":" not in token:
        sys.exit("error: that does not look like a bot token")

    print("  3. Now open your new bot in Telegram and send it any message (e.g. hi).")
    input("     Press Enter once you have sent it: ")

    cfg.save({"token": token, "chat_id": 0, "offset": 0})
    try:
        resp = telegram_poller._api(token, "getUpdates", {"offset": 0, "timeout": 0}, 10)
    except Exception as e:
        sys.exit(f"error: could not reach Telegram: {e}")
    if not resp.get("ok"):
        sys.exit("error: Telegram rejected the token")

    chats = [
        (u.get("message") or u.get("channel_post") or {}).get("chat", {}).get("id")
        for u in resp.get("result", [])
    ]
    chats = [c for c in chats if c]
    if not chats:
        sys.exit("error: no message seen yet. Send your bot a message, then re-run this.")

    chat_id = chats[-1]
    offset = max(u["update_id"] for u in resp["result"]) + 1
    cfg.save({"token": token, "chat_id": chat_id, "offset": offset, "aliases": {}, "status": None})
    print(f"  linked to chat {chat_id}")

    try:
        import install_autostart

        install_autostart.install_poller()
        print("  background poller installed (every 15 minutes)")
    except Exception as e:
        print(f"  note: could not install the background poller automatically ({e}).")
        print(f"  Run it manually any time with: python3 {Path(__file__).parent / 'telegram_poller.py'}")

    print("\nDone. Send your bot a link; it will show up in the From Telegram column.")
    print("Tip: prefix a message with #<board> (e.g. #workboard) to send it straight to that board.")
    print("Set a short alias with: card.py telegram-alias qm /path/to/project/board")


def cmd_telegram_alias(args):
    """Add or remove a custom short alias for a board."""
    import _tg_config as cfg

    conf = cfg.load()
    if not conf:
        sys.exit("error: run `card.py telegram-setup` first")
    aliases = dict(conf.get("aliases") or {})
    if args.rm:
        aliases.pop(args.alias, None)
        print(f"removed alias #{args.alias}")
    else:
        if not args.board_dir:
            sys.exit("error: give the board dir, e.g. card.py telegram-alias qm ~/Desktop/QM/board")
        p = Path(args.board_dir).expanduser().resolve()
        if not (p / "board.json").exists():
            sys.exit(f"error: no board.json in {p}")
        aliases[args.alias] = str(p)
        print(f"#{args.alias} -> {p}")
    conf["aliases"] = aliases
    cfg.save(conf)
```

Ensure `sys` and `Path` are imported at the top of `card_commands.py` (they already are; confirm before relying on them).

- [ ] **Step 4: Register the subcommands in `card.py`**

In `scripts/card.py`, immediately before `return ap` (currently line 387):

```python
    pib = sub.add_parser("inbox", help="list unclaimed Telegram captures")
    pib.set_defaults(fn=cmd_inbox)

    pcl = sub.add_parser("claim", help="promote a Telegram capture to a card on this board")
    pcl.add_argument("tid", help="inbox item id, e.g. T-7")
    pcl.add_argument("--column", help="target column (default: the board's task column)")
    pcl.set_defaults(fn=cmd_claim)

    pts = sub.add_parser("telegram-setup", help="connect a Telegram bot for phone capture")
    pts.add_argument("--token", help="bot token (otherwise prompted)")
    pts.set_defaults(fn=cmd_telegram_setup)

    pta = sub.add_parser("telegram-alias", help="set a short #alias for a board")
    pta.add_argument("alias")
    pta.add_argument("board_dir", nargs="?")
    pta.add_argument("--rm", action="store_true", help="remove the alias")
    pta.set_defaults(fn=cmd_telegram_alias)
```

Add `"inbox"` to `_READ_ONLY_CMDS` (card.py:401-404) so listing captures does not move the last-active board pointer.

In `main()` (card.py:616-617), extend the board-less dispatch next to `board-new`:

```python
    if args.cmd == "board-new":
        return cmd_board_new(args)
    if args.cmd in ("telegram-setup", "telegram-alias"):
        return args.fn(args)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 dev/test_856_cli_claim.py`
Expected: `PASS`

Run: `python3 scripts/card.py inbox`
Expected: `inbox empty - nothing captured from Telegram` (exit 0)

- [ ] **Step 6: Commit**

```bash
git add scripts/card_commands.py scripts/card.py dev/test_856_cli_claim.py
git commit -m "feat(#856): card.py inbox/claim/telegram-setup/telegram-alias"
```

---

### Task 6: Server endpoints

**Files:**
- Modify: `scripts/serve.py` (add `elif path == "/inbox"` to `do_GET` around line 658; add the three POST routes before the `if path != "/board.json"` guard at line 962; add four handler methods near `_handle_boards` at line 838)
- Test: `dev/test_856_serve_inbox.py`

**Interfaces:**
- Consumes: `_inbox` (Task 1), `card_state.build_card` (Task 2), `_tg_config.task_column` (Task 3), plus the existing `broadcast`, `_boardio.board_lock`, `_boardio.write_backup`, `regen_index`, `atomic_write`.
- Produces:
  - `GET /inbox` -> `{"items": [...], "configured": bool}` (pure read, no side effects)
  - `POST /inbox/claim` `{"tid": "T-7", "column": "notes"}` -> `200 {"ok": true, "card": {...}, "rev": N}` or `409 {"ok": false, "conflict": true, "claim": {...}}` or `404`
  - `POST /inbox/discard` `{"tid": "T-7"}` -> `200 {"ok": true}`
  - `POST /inbox/notify` -> `200 {"ok": true}` (re-reads the inbox and broadcasts `inbox-updated`; used by the poller and the CLI)
  - SSE event `inbox-updated` with payload `{"items": [...]}`
- Claim ordering (this is the correctness core): **reserve the inbox item first, then create the card under the board lock, then finalize. On any failure, release.** Never hold the inbox lock while taking the board lock.

- [ ] **Step 1: Write the failing test**

Create `dev/test_856_serve_inbox.py`:

```python
#!/usr/bin/env python3
"""#856 - server inbox endpoints.

Run: python3 dev/test_856_serve_inbox.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856serve-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")

import _inbox  # noqa: E402
import serve  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


class _Cap:
    def __init__(self):
        self.status = None
        self.body = b""

    def __call__(self, status, body, ctype="application/json", extra=None):
        self.status = status
        self.body = body

    def json(self):
        return json.loads(self.body.decode())


def mk_board():
    bd = Path(tempfile.mkdtemp(prefix="t856sb-")) / "board"
    bd.mkdir(parents=True)
    (bd / "board.json").write_text(json.dumps({
        "title": "T", "rev": 3, "nextNum": 20, "schemaVersion": 3,
        "columns": [{"id": "notes", "name": "Notes"}, {"id": "task", "name": "Task"}],
        "cards": [], "activeWork": None,
    }))
    return bd


def handler(board_dir, cap, body: dict | None = None, path="/inbox"):
    h = serve.BoardHandler.__new__(serve.BoardHandler)
    h.board_dir = board_dir
    serve.BoardHandler.port = 7999
    h.path = path
    h._send = cap
    raw = json.dumps(body or {}).encode()
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = BytesIO(raw)
    return h


def reset_inbox():
    p = _inbox.path()
    if p.exists():
        p.unlink()


def test_get_inbox_is_pure_read():
    print("GET /inbox")
    reset_inbox()
    bd = mk_board()
    _inbox.append("idea one", update_id=1)
    _inbox.append("idea two", update_id=2)
    before = (bd / "board.json").read_text()

    cap = _Cap()
    handler(bd, cap)._handle_inbox()
    check(cap.status == 200, "200")
    check(len(cap.json()["items"]) == 2, "returns both unclaimed items")
    check((bd / "board.json").read_text() == before, "GET has no side effects on board.json")


def test_claim_creates_card():
    print("POST /inbox/claim")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("claim me https://ex.com/x", update_id=3)
    events = []
    real = serve.broadcast
    serve.broadcast = lambda name, data: events.append(name)
    try:
        cap = _Cap()
        handler(bd, cap, {"tid": it["tid"], "column": "notes"})._handle_inbox_claim()
    finally:
        serve.broadcast = real

    check(cap.status == 200, f"200 (got {cap.status})")
    card = cap.json()["card"]
    check(card["column"] == "notes", "card lands in the dropped-on column")
    check("from-telegram" in card["tags"], "tagged from-telegram")
    check(card["origin"] == "claim me https://ex.com/x", "origin is the verbatim message")

    d = json.loads((bd / "board.json").read_text())
    check(len(d["cards"]) == 1, "card persisted to board.json")
    check(d["rev"] == 4, "rev bumped")
    check(d["nextNum"] == 21, "nextNum bumped")
    check("card-added" in events, "card-added broadcast")
    check("inbox-updated" in events, "inbox-updated broadcast")

    check(_inbox.get(it["tid"])["status"] == "claimed", "inbox item claimed")
    check(_inbox.unclaimed() == [], "item leaves the global column")


def test_double_claim_409():
    print("claim race")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("contested", update_id=4)
    real = serve.broadcast
    serve.broadcast = lambda name, data: None
    try:
        cap1 = _Cap()
        handler(bd, cap1, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
        cap2 = _Cap()
        handler(bd, cap2, {"tid": it["tid"], "column": "task"})._handle_inbox_claim()
    finally:
        serve.broadcast = real

    check(cap1.status == 200, "first claim wins")
    check(cap2.status == 409, f"second claim gets 409 (got {cap2.status})")
    check(cap2.json()["claim"]["cardNum"] == cap1.json()["card"]["num"],
          "409 names the winning card so the UI can toast it")
    d = json.loads((bd / "board.json").read_text())
    check(len(d["cards"]) == 1, "no duplicate card created")


def test_claim_unknown_tid_404():
    print("unknown tid")
    reset_inbox()
    bd = mk_board()
    cap = _Cap()
    handler(bd, cap, {"tid": "T-999", "column": "task"})._handle_inbox_claim()
    check(cap.status == 404, f"404 (got {cap.status})")


def test_discard():
    print("POST /inbox/discard")
    reset_inbox()
    bd = mk_board()
    it = _inbox.append("junk", update_id=5)
    real = serve.broadcast
    events = []
    serve.broadcast = lambda name, data: events.append(name)
    try:
        cap = _Cap()
        handler(bd, cap, {"tid": it["tid"]})._handle_inbox_discard()
    finally:
        serve.broadcast = real
    check(cap.status == 200, "200")
    check(_inbox.get(it["tid"])["status"] == "discarded", "item discarded")
    check("inbox-updated" in events, "inbox-updated broadcast")
    d = json.loads((bd / "board.json").read_text())
    check(d["cards"] == [], "discard never creates a card")


def test_notify_broadcasts():
    print("POST /inbox/notify")
    reset_inbox()
    bd = mk_board()
    _inbox.append("fresh", update_id=6)
    events = []
    real = serve.broadcast
    serve.broadcast = lambda name, data: events.append((name, data))
    try:
        cap = _Cap()
        handler(bd, cap, {})._handle_inbox_notify()
    finally:
        serve.broadcast = real
    check(cap.status == 200, "200")
    names = [n for n, _ in events]
    check("inbox-updated" in names, "broadcasts inbox-updated")
    payload = dict(events)["inbox-updated"]
    check(len(payload["items"]) == 1, "payload carries the unclaimed items")


if __name__ == "__main__":
    test_get_inbox_is_pure_read()
    test_claim_creates_card()
    test_double_claim_409()
    test_claim_unknown_tid_404()
    test_discard()
    test_notify_broadcasts()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev/test_856_serve_inbox.py`
Expected: FAIL with `AttributeError: 'BoardHandler' object has no attribute '_handle_inbox'`

- [ ] **Step 3: Add the handlers to `serve.py`**

Add these methods to `BoardHandler`, immediately after `_handle_boards` (around line 863):

```python
    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def _broadcast_inbox(self) -> None:
        import _inbox

        broadcast("inbox-updated", {"items": _inbox.unclaimed()})

    def _handle_inbox(self):
        """Pure read. The virtual column's contents."""
        import _inbox
        import _tg_config as tgcfg

        payload = {"items": _inbox.unclaimed(), "configured": tgcfg.load() is not None}
        self._send(200, json.dumps(payload).encode())

    def _handle_inbox_claim(self):
        """Materialize an inbox capture as a real card on THIS board.

        Order matters: reserve the item (atomic CAS) BEFORE touching the board,
        so two boards racing on the same item cannot both create a card. If the
        board write then fails, release the reservation.
        """
        import _inbox
        import _tg_config as tgcfg
        from card_state import build_card

        body = self._read_body()
        tid = body.get("tid")
        if not tid or not _inbox.get(tid):
            self._send(404, json.dumps({"ok": False, "error": "no such item"}).encode())
            return

        try:
            item = _inbox.reserve(tid)
        except _inbox.InboxConflict as e:
            self._send(409, json.dumps(
                {"ok": False, "conflict": True, "claim": e.claim}).encode())
            return

        token = item["reserveToken"]
        bp = self.board_dir / "board.json"
        try:
            with _boardio.board_lock(bp):
                d = json.loads(bp.read_text())
                column = body.get("column") or tgcfg.task_column(d)
                card = build_card(
                    d,
                    title=item["title"],
                    column=column,
                    tags=["from-telegram"],
                    origin=item["text"],
                    meta={"telegram": {"tid": tid, "updateId": item.get("update_id"),
                                       "capturedAt": item.get("ts")}},
                )
                d["rev"] = int(d.get("rev", 0)) + 1
                d["savedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                d["savedBy"] = "telegram"
                out = json.dumps(d, indent=2).encode()
                atomic_write(bp, out)
                _boardio.write_backup(bp, out)
                regen_index(self.board_dir)
                _refresh_cache(self.board_dir)
        except Exception as e:
            _inbox.release(tid, token)  # never strand a reservation
            self._send(500, json.dumps({"ok": False, "error": str(e)}).encode())
            return

        _inbox.finalize(tid, board=str(self.board_dir), card_num=card["num"], token=token)
        broadcast("card-added", {"card": card})
        broadcast("rev-bumped", {"rev": d["rev"], "savedBy": "telegram",
                                 "savedAt": d["savedAt"], "activeWork": d.get("activeWork")})
        self._broadcast_inbox()
        self._send(200, json.dumps({"ok": True, "card": card, "rev": d["rev"]}).encode())

    def _handle_inbox_discard(self):
        import _inbox

        body = self._read_body()
        tid = body.get("tid")
        if not tid or not _inbox.get(tid):
            self._send(404, json.dumps({"ok": False, "error": "no such item"}).encode())
            return
        _inbox.discard(tid)
        self._broadcast_inbox()
        self._send(200, json.dumps({"ok": True}).encode())

    def _handle_inbox_notify(self):
        """The poller or the CLI captured/claimed something: push it to open boards."""
        self._read_body()  # drain, keep-alive framing
        self._broadcast_inbox()
        self._send(200, json.dumps({"ok": True}).encode())
```

Wire the routes. In `do_GET`, after the `/boards` branch (line 655-656):

```python
        elif path == "/inbox":
            self._handle_inbox()
```

In `do_POST`, immediately before the `if path != "/board.json":` guard (line 962):

```python
        if path == "/inbox/claim":
            self._handle_inbox_claim()
            return
        if path == "/inbox/discard":
            self._handle_inbox_discard()
            return
        if path == "/inbox/notify":
            self._handle_inbox_notify()
            return
```

Check the imports at the top of `serve.py`: it must have `_boardio`, `regen_index`, `atomic_write`, and `datetime`/`timezone`. Add whatever is missing (`_refresh_cache` is the existing helper that repopulates `_cached_state` after a write; use the real name found at the `POST /board.json` path around line 1015 rather than inventing one).

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 dev/test_856_serve_inbox.py`
Expected: `PASS`

- [ ] **Step 5: Commit**

```bash
git add scripts/serve.py dev/test_856_serve_inbox.py
git commit -m "feat(#856): server inbox endpoints - virtual column read, atomic claim, discard"
```

---

### Task 7: The virtual column in the UI

**Files:**
- Modify: `templates/board.html` (render, drag, SSE, toast)
- Modify: `templates/board.json` (add `from-telegram` to the tag taxonomy)
- Test: `dev/test_856_ui_markup.py`

**Interfaces:**
- Consumes: `GET /inbox`, `POST /inbox/claim`, `POST /inbox/discard`, SSE `inbox-updated` (Task 6).
- Produces (JS globals/functions other code in board.html relies on):
  - `INBOX_COL = '__inbox__'` (the virtual column id; never persisted)
  - `let inboxItems = []`
  - `fetchInbox()`, `renderInboxColumn()`, `commitInboxClaim()`, `discardInboxItem(tid)`
- Hard rule (repeat of the global constraint, because this is where it would break): inbox items must never be pushed into `state.cards` or `state.columns`. `save()` serializes `state` wholesale.

- [ ] **Step 1: Write the failing test**

Create `dev/test_856_ui_markup.py`:

```python
#!/usr/bin/env python3
"""#856 - virtual column markup + wiring (static checks on board.html).

Run: python3 dev/test_856_ui_markup.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HTML = (REPO / "templates" / "board.html").read_text()

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def test_wiring():
    print("virtual column wiring")
    check("__inbox__" in HTML, "virtual column id present")
    check("fetchInbox" in HTML, "fetchInbox defined")
    check("renderInboxColumn" in HTML, "renderInboxColumn defined")
    check("commitInboxClaim" in HTML, "commitInboxClaim defined")
    check("discardInboxItem" in HTML, "discardInboxItem defined")
    check("/inbox/claim" in HTML, "claim endpoint called")
    check("/inbox/discard" in HTML, "discard endpoint called")
    check("inbox-updated" in HTML, "SSE inbox-updated listener registered")
    check("addEventListener('focus'" in HTML or 'addEventListener("focus"' in HTML,
          "refetches on window focus")


def test_never_persisted():
    """The single most dangerous bug: a virtual item leaking into state and being saved."""
    print("virtual items never enter state")
    check("state.cards.push(...inboxItems" not in HTML, "inbox items not pushed into state.cards")
    check("state.columns.push({ id: INBOX_COL" not in HTML, "virtual column not pushed into state.columns")
    check(re.search(r"inboxItems\s*=\s*\[\]", HTML) is not None,
          "inboxItems is its own array, separate from state")


def test_claim_branch_precedes_card_move():
    print("drag branch")
    m = re.search(r"function commitCardDrag\s*\([^)]*\)\s*\{(.{0,400})", HTML, re.S)
    check(m is not None, "commitCardDrag found")
    if m:
        head = m.group(1)
        check("inboxTid" in head,
              "commitCardDrag branches on an inbox drag BEFORE looking the card up in state.cards")


def test_taxonomy():
    print("tag taxonomy")
    tax = (REPO / "templates" / "board.json").read_text()
    check("from-telegram" in tax, "from-telegram registered in the new-board tag taxonomy")


if __name__ == "__main__":
    test_wiring()
    test_never_persisted()
    test_claim_branch_precedes_card_move()
    test_taxonomy()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev/test_856_ui_markup.py`
Expected: FAIL (`virtual column id present`, etc.)

- [ ] **Step 3: Add the styles**

In the `<style>` block of `templates/board.html`, next to the existing `.column` rules:

```css
    .column.is-inbox { border: 1px dashed var(--border, #3a3a38); background: rgba(120,160,200,.05); }
    .column.is-inbox .column-header { opacity: .9; }
    .card.is-inbox { cursor: grab; border-left: 3px solid #4A7BA5; }
    .card.is-inbox .num-ref { color: #4A7BA5; font-weight: 600; }
    .card.is-inbox.is-aging { opacity: .55; }
    .card.is-inbox .inbox-discard {
      opacity: 0; margin-left: auto; padding: 0 4px; border: 0; background: none;
      color: var(--muted, #8B8680); cursor: pointer; font-size: 13px; line-height: 1;
    }
    .card.is-inbox:hover .inbox-discard { opacity: .7; }
    .card.is-inbox .inbox-discard:hover { opacity: 1; color: #C84B4B; }
    .inbox-empty { padding: 10px 12px; color: var(--muted, #8B8680); font-size: 12px; line-height: 1.5; }
```

- [ ] **Step 4: Add the JS**

Near the other module globals (by `let _cardDrag = null;`):

```js
const INBOX_COL = '__inbox__';   // virtual: never written to state.columns / board.json
let inboxItems = [];
let inboxConfigured = false;
```

Add the fetch + render + claim + discard block (put it next to `renderColumn`):

```js
async function fetchInbox() {
  try {
    const r = await fetch(apiBase + '/inbox');
    if (!r.ok) return;
    const data = await r.json();
    inboxItems = data.items || [];
    inboxConfigured = !!data.configured;
    renderInboxColumn();
  } catch (_) { /* board offline: the column just stays as it was */ }
}

function inboxAgeDays(ts) {
  const t = Date.parse(ts);
  return Number.isNaN(t) ? 0 : (Date.now() - t) / 86400000;
}

function renderInboxCard(item) {
  const el = document.createElement('div');
  el.className = 'card is-inbox' + (inboxAgeDays(item.ts) > 7 ? ' is-aging' : '');
  el.dataset.inboxTid = item.tid;
  el.draggable = true;

  const head = document.createElement('div');
  head.className = 'card-head';
  const ref = document.createElement('span');
  ref.className = 'num-ref';
  ref.textContent = item.tid;
  head.appendChild(ref);

  const x = document.createElement('button');
  x.className = 'inbox-discard';
  x.title = 'Discard this capture';
  x.textContent = '×';
  x.addEventListener('click', (e) => { e.stopPropagation(); discardInboxItem(item.tid); });
  head.appendChild(x);
  el.appendChild(head);

  const title = document.createElement('div');
  title.className = 'card-title clamp';
  title.textContent = item.title;
  el.appendChild(title);

  el.addEventListener('dragstart', (e) => {
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', 'inbox:' + item.tid);
    _cardDrag = {
      inboxTid: item.tid, cardId: null, srcCol: INBOX_COL, srcEl: el,
      hoverCol: null, hoverBeforeCardId: null,
    };
    el.classList.add('dragging');
  });
  el.addEventListener('dragend', () => {
    el.classList.remove('dragging');
    if (_cardDrag && _cardDrag.inboxTid) cancelCardDrag();
  });
  return el;
}

function renderInboxColumn() {
  const boardEl = document.getElementById('board');
  if (!boardEl) return;
  const existing = document.getElementById('inbox-column');
  if (!inboxConfigured && !inboxItems.length) { if (existing) existing.remove(); return; }

  const col = document.createElement('div');
  col.className = 'column is-inbox';
  col.id = 'inbox-column';
  col.dataset.col = INBOX_COL;

  const header = document.createElement('div');
  header.className = 'column-header';
  header.textContent = `📥 From Telegram (${inboxItems.length})`;
  col.appendChild(header);

  const body = document.createElement('div');
  body.className = 'column-body';
  if (!inboxItems.length) {
    const empty = document.createElement('div');
    empty.className = 'inbox-empty';
    empty.textContent = 'Send a link to your bot from your phone; it lands here.';
    body.appendChild(empty);
  } else {
    inboxItems.forEach((it) => body.appendChild(renderInboxCard(it)));
  }
  col.appendChild(body);

  // Real cards may not be dropped INTO the virtual column: it is not a real column.
  col.addEventListener('dragover', (e) => {
    if (_cardDrag && _cardDrag.inboxTid) e.preventDefault();
  });

  if (existing) existing.replaceWith(col);
  else boardEl.insertBefore(col, boardEl.firstChild);
}

async function commitInboxClaim() {
  const tid = _cardDrag && _cardDrag.inboxTid;
  const column = _cardDrag && _cardDrag.hoverCol;
  _cardDrag = null;
  if (!tid || !column || column === INBOX_COL) { renderInboxColumn(); return; }
  try {
    const r = await fetch(apiBase + '/inbox/claim', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid, column }),
    });
    const data = await r.json();
    if (r.status === 409) {
      const c = data.claim || {};
      toast(`Already claimed elsewhere (#${c.cardNum || '?'})`);
    } else if (r.ok) {
      toast(`Claimed ${tid} → #${data.card.num}`);
      // The real card arrives over SSE (card-added); just drop it from the virtual column.
    } else {
      toast('Could not claim that capture');
    }
  } catch (_) {
    toast('Could not claim that capture');
  }
  fetchInbox();
}

async function discardInboxItem(tid) {
  try {
    await fetch(apiBase + '/inbox/discard', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tid }),
    });
    toast(`Discarded ${tid}`);
  } catch (_) { /* ignore */ }
  fetchInbox();
}
```

In `commitCardDrag()`, as the **first** statement (before `state.cards.find(...)`, which would otherwise silently bail on an unknown id):

```js
  if (_cardDrag && _cardDrag.inboxTid) { commitInboxClaim(); return; }
```

In `render()`, after the columns are built, re-attach the virtual column:

```js
  renderInboxColumn();
```

In `startSSE()`, alongside the other listeners:

```js
  _es.addEventListener('inbox-updated', (e) => {
    try {
      const d = JSON.parse(e.data);
      inboxItems = d.items || [];
      if (isDragInProgress()) { _pendingResync = true; return; }
      renderInboxColumn();
    } catch (_) { /* ignore */ }
  });
```

At the end of the boot sequence (where the board first renders), and on focus:

```js
  fetchInbox();
  window.addEventListener('focus', fetchInbox);
```

- [ ] **Step 5: Add the tag to the new-board taxonomy**

In `templates/board.json`, add to the `tagTaxonomy.sub` array:

```json
        { "name": "from-telegram", "color": "#4A7BA5" }
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `python3 dev/test_856_ui_markup.py`
Expected: `PASS`

- [ ] **Step 7: Verify in a real browser (do not skip)**

```bash
python3 - <<'PY'
import os, sys
sys.path.insert(0, 'scripts')
os.environ.setdefault('BOARD_INBOX', os.path.expanduser('~/.board-steward/inbox.jsonl'))
import _inbox
_inbox.append('https://example.com/a-security-video walk through', update_id=999001)
_inbox.append('#workboard tidy the drag preview', update_id=999002)
_inbox.notify_boards()
print('seeded')
PY
```

Then open `http://127.0.0.1:7891` and confirm, with your own eyes:
1. The "📥 From Telegram" column appears, leftmost, with both items and `T-n` badges.
2. Dragging an item into "Notes" creates a real card there, and the item vanishes from the virtual column.
3. The toast reads `Claimed T-n → #<num>`.
4. The new card carries the `from-telegram` tag, and its tooltip/origin shows the verbatim message.
5. A second board (e.g. `:7893`) open at the same time drops the item from its virtual column too, live.
6. Dragging a *real* card onto the virtual column is refused (no drop, no state change).
7. The `×` discards an item and it disappears everywhere.
8. Nothing about the virtual column leaks into `board.json`: `git diff` on the board file shows only the claimed card.

Clean up the seeds afterwards (discard them from the UI, or claim and delete the cards).

- [ ] **Step 8: Commit**

```bash
git add templates/board.html templates/board.json dev/test_856_ui_markup.py
git commit -m "feat(#856): virtual From Telegram column with drag-to-claim"
```

---

### Task 8: Background poller job (launchd + systemd)

**Files:**
- Modify: `scripts/install_launchd.py` (add `build_poller_plist`, `install_poller`, `uninstall_poller`)
- Modify: `scripts/install_systemd.py` (add a service + 15-minute timer unit)
- Modify: `scripts/install_autostart.py` (add `install_poller()` dispatching by platform; on unsupported platforms print the manual command instead of failing)
- Test: `dev/test_856_poller_job.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure installer code). `cmd_telegram_setup` (Task 5) already calls `install_autostart.install_poller()`.
- Produces:
  - `install_launchd.build_poller_plist(poller_py: Path) -> dict`
  - `install_launchd.install_poller(dry_run: bool = False) -> Path`
  - `install_autostart.install_poller() -> None`
- The poller job is **global, not per board** (there is one inbox and one bot), so its label is `com.boardsteward.telegram` with no port suffix.

- [ ] **Step 1: Write the failing test**

Create `dev/test_856_poller_job.py`:

```python
#!/usr/bin/env python3
"""#856 - the 15-minute poller job.

Run: python3 dev/test_856_poller_job.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import install_launchd as il  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def test_plist_shape():
    print("poller plist")
    p = il.build_poller_plist(REPO / "scripts" / "telegram_poller.py")
    check(p["Label"] == "com.boardsteward.telegram", "one global label, not per board")
    check(p.get("StartInterval") == 900, "fires every 15 minutes")
    check("KeepAlive" not in p,
          "KeepAlive must NOT be set: launchd would respawn the short-lived poller in a tight loop")
    check(str(p["ProgramArguments"][-1]).endswith("telegram_poller.py"), "runs the poller")
    check("Logs" in p["StandardErrorPath"] or "log" in p["StandardErrorPath"].lower(),
          "errors are logged somewhere findable")


def test_install_is_idempotent_dry_run():
    print("dry run")
    a = il.install_poller(dry_run=True)
    b = il.install_poller(dry_run=True)
    check(a == b, "same plist path each time")
    check(str(a).endswith("com.boardsteward.telegram.plist"), "plist path derived from the label")


if __name__ == "__main__":
    test_plist_shape()
    test_install_is_idempotent_dry_run()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev/test_856_poller_job.py`
Expected: FAIL with `AttributeError: module 'install_launchd' has no attribute 'build_poller_plist'`

- [ ] **Step 3: Add the launchd poller job**

In `scripts/install_launchd.py`, after the existing `build_plist`:

```python
POLLER_LABEL = "com.boardsteward.telegram"  # one bot, one inbox: a single global job


def poller_plist_path() -> Path:
    return PLIST_DIR / f"{POLLER_LABEL}.plist"


def build_poller_plist(poller_py: Path) -> dict:
    """A 15-minute interval job, NOT a KeepAlive daemon.

    KeepAlive must stay unset: the poller exits after each cycle, and launchd
    would respawn it in a tight loop.
    """
    return {
        "Label": POLLER_LABEL,
        "ProgramArguments": [find_python(), str(poller_py)],
        "RunAtLoad": True,
        "StartInterval": 900,
        "StandardOutPath": str(LOG_DIR / "telegram-poller.out.log"),
        "StandardErrorPath": str(LOG_DIR / "telegram-poller.err.log"),
    }


def install_poller(dry_run: bool = False) -> Path:
    poller_py = Path(__file__).resolve().parent / "telegram_poller.py"
    plist = build_poller_plist(poller_py)
    target = poller_plist_path()
    if dry_run:
        return target
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True)
    with target.open("wb") as fh:
        plistlib.dump(plist, fh)
    subprocess.run(["launchctl", "load", str(target)], capture_output=True)
    return target


def uninstall_poller() -> None:
    target = poller_plist_path()
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True)
    if target.exists():
        target.unlink()
```

- [ ] **Step 4: Add the systemd timer**

In `scripts/install_systemd.py`, mirroring the existing unit writer:

```python
POLLER_UNIT = "boardsteward-telegram"


def install_poller() -> None:
    """A oneshot service plus a 15-minute timer."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    poller_py = Path(__file__).resolve().parent / "telegram_poller.py"

    (unit_dir / f"{POLLER_UNIT}.service").write_text(
        "[Unit]\n"
        "Description=board-steward Telegram capture poller\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={find_python()} {poller_py}\n"
    )
    (unit_dir / f"{POLLER_UNIT}.timer").write_text(
        "[Unit]\n"
        "Description=Poll Telegram for captured ideas every 15 minutes\n\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        "OnUnitActiveSec=15min\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{POLLER_UNIT}.timer"],
                   capture_output=True)
```

- [ ] **Step 5: Dispatch by platform**

In `scripts/install_autostart.py`:

```python
def install_poller() -> None:
    """Install the 15-minute Telegram capture job for this platform."""
    if sys.platform == "darwin":
        import install_launchd

        install_launchd.install_poller()
        return
    if sys.platform.startswith("linux"):
        import install_systemd

        install_systemd.install_poller()
        return
    poller = Path(__file__).resolve().parent / "telegram_poller.py"
    raise RuntimeError(
        f"no scheduler integration for {sys.platform}; schedule this every 15 minutes: "
        f"python3 {poller}"
    )
```

(`cmd_telegram_setup` already catches that `RuntimeError` and prints the manual command, so Windows degrades to a clear instruction rather than a failure.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 dev/test_856_poller_job.py`
Expected: `PASS`

- [ ] **Step 7: Commit**

```bash
git add scripts/install_launchd.py scripts/install_systemd.py scripts/install_autostart.py dev/test_856_poller_job.py
git commit -m "feat(#856): 15-minute capture poller job (launchd + systemd)"
```

---

### Task 9: Session-start digest line and catch-up poll

**Files:**
- Modify: `scripts/hook_session_start.sh` (add an `inbox_line` next to the existing `pending_line` / `recon_line`, and a backgrounded catch-up poll)
- Test: `dev/test_856_hook_line.py`

**Interfaces:**
- Consumes: `_inbox._hook_line()` and `_tg_config.load()["status"]` (Tasks 1 and 3).
- Produces: one extra line in the session-start block, and a best-effort poll that does not slow session start.

- [ ] **Step 1: Write the failing test**

Create `dev/test_856_hook_line.py`:

```python
#!/usr/bin/env python3
"""#856 - session-start digest line.

Run: python3 dev/test_856_hook_line.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856hook-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")

import _inbox  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def hook_line() -> str:
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "_inbox.py"), "--hook-line"],
        capture_output=True, text=True, env={**os.environ},
    )
    return r.stdout.strip()


def test_silent_when_empty():
    print("empty inbox")
    p = _inbox.path()
    if p.exists():
        p.unlink()
    check(hook_line() == "", "no line when there is nothing to claim")


def test_reports_unclaimed():
    print("unclaimed captures")
    _inbox.append("an idea", update_id=1)
    _inbox.append("another", update_id=2)
    line = hook_line()
    check("2 unclaimed" in line, f"reports the count ({line})")
    check("claim" in line.lower(), "tells the agent how to act on them")


def test_hook_script_wires_it():
    print("hook wiring")
    sh = (REPO / "scripts" / "hook_session_start.sh").read_text()
    check("_inbox.py" in sh and "--hook-line" in sh, "hook calls _inbox.py --hook-line")
    check("${inbox_line}" in sh, "inbox_line interpolated into the session block")
    check("telegram_poller.py" in sh, "hook fires a catch-up poll")
    check("&" in sh.split("telegram_poller.py")[1].split("\n")[0],
          "the catch-up poll is backgrounded so session start stays fast")


if __name__ == "__main__":
    test_silent_when_empty()
    test_reports_unclaimed()
    test_hook_script_wires_it()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 dev/test_856_hook_line.py`
Expected: FAIL on `hook calls _inbox.py --hook-line`

- [ ] **Step 3: Wire the hook**

In `scripts/hook_session_start.sh`, just before the final `cat <<MSG` block (next to where `recon_line` is built):

```bash
# Telegram capture (#856): surface unclaimed phone captures, and fire a catch-up
# poll in the background so a machine that was asleep still pulls the last 24h
# without slowing session start.
inbox_line=""
inbox_py="$(dirname "$0")/_inbox.py"
if [ -f "${inbox_py}" ]; then
  inbox_line="$(python3 "${inbox_py}" --hook-line 2>/dev/null)"
fi
poller_py="$(dirname "$0")/telegram_poller.py"
if [ -f "${poller_py}" ]; then
  (python3 "${poller_py}" >/dev/null 2>&1 &)
fi
```

and add `${inbox_line}` to the heredoc, after `${recon_line}`:

```bash
${pending_line}
${recon_line}
${inbox_line}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 dev/test_856_hook_line.py`
Expected: `PASS`

Sanity-check the hook still runs clean and fast:
Run: `time bash scripts/hook_session_start.sh < /dev/null`
Expected: exit 0, the session block prints, well under one second.

- [ ] **Step 5: Commit**

```bash
git add scripts/hook_session_start.sh dev/test_856_hook_line.py
git commit -m "feat(#856): session-start capture digest + backgrounded catch-up poll"
```

---

### Task 10: End-to-end proof, docs, and the ship gate

**Files:**
- Create: `dev/test_856_e2e.py`
- Modify: `README.md` (a short "Capture from your phone" section)
- Modify: `SKILL.md` (one line: captures land in the From Telegram column; claim them with `card.py claim`)
- Modify: `docs/PLAYBOOK.md` (setup walkthrough)

**Interfaces:**
- Consumes: everything above.
- Produces: an isolated end-to-end test that proves the whole chain without touching the live board or the real inbox.

- [ ] **Step 1: Write the end-to-end test**

Create `dev/test_856_e2e.py`:

```python
#!/usr/bin/env python3
"""#856 - end to end: fake Telegram -> poller -> inbox -> claim -> real card.

Everything is isolated: a temp inbox, a temp config, a throwaway board. The
live board and the real inbox must be untouched.

Run: python3 dev/test_856_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856e2e-"))
os.environ["BOARD_INBOX"] = str(_STATE / "inbox.jsonl")
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")
Path(os.environ["BOARD_REGISTRY"]).write_text("{}")

LIVE_BOARD = Path.home() / "Desktop" / "WorkBoard" / "board" / "board.json"
LIVE_BEFORE = LIVE_BOARD.read_text() if LIVE_BOARD.exists() else None
LIVE_INBOX = Path.home() / ".board-steward" / "inbox.jsonl"
LIVE_INBOX_BEFORE = LIVE_INBOX.read_text() if LIVE_INBOX.exists() else None

import _inbox  # noqa: E402
import _tg_config as cfg  # noqa: E402
import serve  # noqa: E402
import telegram_poller as tp  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


class _Cap:
    def __init__(self):
        self.status = None
        self.body = b""

    def __call__(self, status, body, ctype="application/json", extra=None):
        self.status = status
        self.body = body

    def json(self):
        return json.loads(self.body.decode())


def mk_board(name):
    bd = Path(tempfile.mkdtemp(prefix=f"t856-{name}-")) / "board"
    bd.mkdir(parents=True)
    (bd / "board.json").write_text(json.dumps({
        "title": name, "rev": 1, "nextNum": 1, "schemaVersion": 3,
        "columns": [{"id": "task", "name": "Task"}, {"id": "notes", "name": "Notes"}],
        "cards": [], "activeWork": None,
    }))
    return bd


def handler(bd, cap, body=None):
    h = serve.BoardHandler.__new__(serve.BoardHandler)
    h.board_dir = bd
    serve.BoardHandler.port = 7999
    h._send = cap
    raw = json.dumps(body or {}).encode()
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = BytesIO(raw)
    return h


def fake_api(updates, sent):
    def api(token, method, params, timeout):
        if method == "getUpdates":
            off = int(params.get("offset", 0))
            return {"ok": True, "result": [u for u in updates if u["update_id"] >= off]}
        sent.append(params.get("text"))
        return {"ok": True}

    return api


def main():
    print("end to end")
    cfg.save({"token": "TOK", "chat_id": 7, "offset": 0})
    board_a, board_b = mk_board("a"), mk_board("b")
    sent = []

    # 1. The phone sends two messages.
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 7}, "text": "https://ex.com/security-video"}},
        {"update_id": 2, "message": {"chat": {"id": 7}, "text": "idea: batch the recon sweep"}},
    ]
    serve.broadcast = lambda name, data: None  # no SSE clients in this harness
    new = tp.poll(api=fake_api(updates, sent))
    check(len(new) == 2, "poller captured both messages")
    check(len(sent) == 2 and all("saved" in s for s in sent), "both got a confirmation reply")

    # 2. Both boards show both items in the virtual column.
    for bd, label in ((board_a, "board A"), (board_b, "board B")):
        cap = _Cap()
        handler(bd, cap)._handle_inbox()
        check(len(cap.json()["items"]) == 2, f"{label} shows both captures")

    # 3. Claim the first item on board A, into its notes column.
    tid = new[0]["tid"]
    cap = _Cap()
    handler(board_a, cap, {"tid": tid, "column": "notes"})._handle_inbox_claim()
    check(cap.status == 200, "claim succeeded")
    card = cap.json()["card"]
    check(card["column"] == "notes", "card landed in the column it was dropped on")
    check("from-telegram" in card["tags"], "card is tagged from-telegram")
    check(card["origin"] == "https://ex.com/security-video", "origin is the verbatim message")

    # 4. It is gone from BOTH boards' virtual columns, and board B never got a card.
    for bd, label in ((board_a, "board A"), (board_b, "board B")):
        cap = _Cap()
        handler(bd, cap)._handle_inbox()
        tids = [i["tid"] for i in cap.json()["items"]]
        check(tid not in tids, f"claimed item gone from {label}")
        check(len(tids) == 1, f"{label} still shows the other capture")
    db = json.loads((board_b / "board.json").read_text())
    check(db["cards"] == [], "board B has no card: claiming is exclusive")

    # 5. Board A really has the card on disk.
    da = json.loads((board_a / "board.json").read_text())
    check(len(da["cards"]) == 1 and da["cards"][0]["num"] == card["num"], "card persisted on board A")

    # 6. Claiming it again is refused.
    cap = _Cap()
    handler(board_b, cap, {"tid": tid, "column": "task"})._handle_inbox_claim()
    check(cap.status == 409, "re-claiming from another board is refused")

    # 7. Nothing leaked into the user's real state.
    if LIVE_BEFORE is not None:
        check(LIVE_BOARD.read_text() == LIVE_BEFORE, "the LIVE board is untouched")
    now_inbox = LIVE_INBOX.read_text() if LIVE_INBOX.exists() else None
    check(now_inbox == LIVE_INBOX_BEFORE, "the REAL inbox is untouched")


if __name__ == "__main__":
    main()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
```

- [ ] **Step 2: Run it**

Run: `python3 dev/test_856_e2e.py`
Expected: `PASS`

- [ ] **Step 3: Run the whole 856 suite plus the existing suite**

Run:
```bash
for t in dev/test_856_*.py; do echo "== $t"; python3 "$t" || exit 1; done
for t in dev/test_*.py; do python3 "$t" >/dev/null 2>&1 || echo "FAILED: $t"; done
```
Expected: every `test_856_*` prints `PASS`; the second loop reports no new failures compared to `main`.

- [ ] **Step 4: Document it**

`README.md`, a new short section:

```markdown
### Capture ideas from your phone

Send a link or a thought to your own Telegram bot and it shows up on your boards.

1. Message [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, copy the token.
2. Run `card.py telegram-setup` and paste it, then message your new bot once when prompted.

Captures land in a **From Telegram** column that appears on every board. Drag one into
any column to turn it into a real card there; it disappears from the other boards.
Prefix a message with `#<board>` (e.g. `#workboard fix the drag preview`) to send it
straight to that board. Set a short alias with `card.py telegram-alias qm ~/path/to/board`.

No server, no hosting, no cost: a local job polls Telegram every 15 minutes and nothing
new listens on a port.
```

`SKILL.md`, one line in the protocol section:

```markdown
- Phone captures land in the **From Telegram** virtual column (shared across boards). Claim one with `card.py claim <T-n> [--column <col>]`; list them with `card.py inbox`.
```

`docs/PLAYBOOK.md`: add the same setup walkthrough with the alias and `#prefix` examples.

- [ ] **Step 5: The real ship gate (do not skip, and do not claim this is done without it)**

With your own phone and your own bot:

1. `python3 scripts/card.py telegram-setup` and complete it.
2. Send the bot a plain link. Confirm the `saved` reply arrives on the phone.
3. Wait for the 15-minute job (or run `python3 scripts/telegram_poller.py` once) and confirm the item appears in the From Telegram column on the open board, live, without a refresh.
4. Drag it into a column. Confirm a real card is created, tagged `from-telegram`, with your verbatim message as its origin.
5. Send `#<alias> <link>` with a real alias. Confirm the bot replies `saved -> <alias> #<num>` and the card flies onto that board.
6. Confirm the item never appears in the From Telegram column of a *different* board after being claimed.

Only after all six pass is #856 shippable.

- [ ] **Step 6: Commit and card**

```bash
git add dev/test_856_e2e.py README.md SKILL.md docs/PLAYBOOK.md
git commit -m "feat(#856): end-to-end capture proof + docs"
```

Then close the card with the real evidence:

```bash
scripts/card.py --board board/board.json fly 856 done \
  --writeup "Telegram phone capture shipped. Poller (getUpdates, launchd 15min, no server, \$0) -> shared ~/.board-steward/inbox.jsonl -> virtual From Telegram column on every board -> drag-to-claim materializes a real card on one board and removes it everywhere (atomic first-wins). #<board> prefix routes instantly; aliases derived per user from their own board registry. Tests: dev/test_856_{inbox,build_card,tg_config,poller,cli_claim,serve_inbox,ui_markup,poller_job,hook_line,e2e}.py all green. Verified end to end from a real phone."
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the poller and its config to Tasks 3 and 4; the inbox and its claim semantics to Task 1; the shared card constructor that keeps claimed cards schema-identical to `card.py add` cards to Task 2; the server endpoints to Task 6; the virtual column, drag-to-claim, discard, toast, and aging cue to Task 7; the per-user alias resolution (registry-derived plus custom, never guessing) to Task 3 and exercised in Task 4; the setup wizard to Task 5; the scheduled job to Task 8; the session digest and catch-up poll to Task 9; the error-handling contract (network down, bad token, stranger messages, claim races, corrupt lines, chmod 600) is asserted across Tasks 1, 3, 4, and 6; the test plan and the real-phone ship gate to Task 10.

**Deliberate deviation from the spec, already amended there.** Route hints are executed by the poller through `card.py claim` rather than lazily by the server during a render. The exploration showed `GET /inbox` would otherwise have to mutate board.json as a side effect of a read, which is both surprising and hard to test. `card.py` is the sanctioned write path and already routes through the running server, so a hinted capture flies onto the board live.

**Known limits, stated rather than hidden.** Windows gets a printed manual command instead of an installed job (Task 8). The 24-hour Telegram queue window remains: if the machine is off for more than a day, an unconfirmed message is dropped by Telegram, which is why the bot's `saved` reply is the capture contract. `unclaimed()` treats a reservation older than 120 seconds as free, which is the self-heal for a claimer that crashed mid-claim.
