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

import contextlib
import fcntl
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
# Anchored to the start of a line: card.py claim prints "+ #431 <title> -> <col> ...",
# and a title could itself contain "#12" - searching the whole blob would grab that
# instead. The real card number is always the first thing on a "+ #N" line.
_NUM_RE = re.compile(r"^\+\s*#(\d+)", re.MULTILINE)
_CLAIM_TIMEOUT = 30.0  # overridable by tests so a hung claim script doesn't stall the suite


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
            capture_output=True, text=True, timeout=_CLAIM_TIMEOUT,
        )
    except Exception:
        return None  # non-zero exit, a hang cut off by the timeout, or any other failure to run
    if r.returncode != 0:
        return None
    m = _NUM_RE.search(r.stdout)
    return f"saved -> {item['routeHint']} #{m.group(1)}" if m else "saved"


def _lock_path() -> Path:
    return cfg.config_path().with_suffix(cfg.config_path().suffix + ".lock")


@contextlib.contextmanager
def _single_instance():
    """Non-blocking flock so two overlapping pollers never run at once.

    A slow session-start catch-up can overlap a scheduled 15-minute tick.
    Both would race `_tg_config.set_offset`, which has no lock of its own.
    Duplicate captures are already impossible (the inbox dedupes on
    update_id), but there is no reason to let a second poller do any work
    at all: it exits silently, yielding False, and the caller returns [].
    """
    lp = _lock_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    fh = lp.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def poll(api=None, timeout: float = 10.0) -> list[dict]:
    """One capture cycle. Returns the newly captured items."""
    with _single_instance() as acquired:
        if not acquired:
            return []  # another poller is already running: don't race it
        return _poll_locked(api, timeout)


def _poll_locked(api, timeout: float) -> list[dict]:
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
        return []  # network down (incl. 429/500): offset untouched, the next tick retries

    # Telegram (or an intermediary) can return {"ok": true, "result": null} or
    # result as a dict/string/int. Validate the whole shape before iterating -
    # anything unexpected is a no-op, exactly like the other malformed-response
    # branches above, rather than an uncaught TypeError that kills the
    # unattended run.
    if not isinstance(resp, dict) or not resp.get("ok"):
        return []
    if conf.get("status"):
        cfg.set_status(None)  # recovered
    result = resp.get("result")
    if not isinstance(result, list):
        return []

    new: list[dict] = []
    max_uid = offset - 1
    for u in result:
        if not isinstance(u, dict):
            continue  # malformed entry: skip, don't raise
        uid = u.get("update_id")
        if uid is None:
            continue
        max_uid = max(max_uid, uid)
        msg = u.get("message") or u.get("channel_post")
        if not isinstance(msg, dict):
            continue  # no message/channel_post, or not shaped like one: skip
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue  # allowlist: anyone can find a bot, only the owner may feed the board
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text or text.startswith("/"):
            continue  # /start and friends are not ideas
        item = _inbox.append(text=text, update_id=uid)
        if item:
            new.append(item)

    if max_uid >= offset:
        cfg.set_offset(max_uid + 1)  # only now: every inbox write above has landed

    # Confirmation is a durable fact on the item, not an accident of loop
    # ordering: this sends to every unconfirmed item, which naturally covers
    # both what was just captured AND anything stranded unconfirmed by an
    # earlier failed tick (e.g. the inbox write for a later item in the same
    # batch raised before any reply went out). mark_confirmed only after the
    # reply actually sends, so a sendMessage failure leaves the capture
    # intact and simply retries next tick - never lost, never duplicated.
    for item in _inbox.unconfirmed():
        reply = _claim_hint(item) or "saved"
        try:
            api(token, "sendMessage", {"chat_id": chat_id, "text": reply}, timeout)
        except Exception:
            continue  # the capture landed; a failed confirmation must not lose it
        _inbox.mark_confirmed(item["tid"])

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
