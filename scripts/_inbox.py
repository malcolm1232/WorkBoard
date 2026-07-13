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
import secrets
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
            "reserveToken": None,
            "confirmed": False,
        }
        items.append(item)
        _write(items)
        return item


def get(tid: str) -> dict | None:
    for i in _read():
        if i["tid"] == tid:
            return i
    return None


def _for_browser(item: dict) -> dict:
    """Drop the reservation token before an item goes to a browser.

    It's a concurrency guard, not a capability, so leaking it isn't a
    security hole - but there's no reason to hand it out either.
    """
    if "reserveToken" not in item:
        return item
    item = dict(item)
    item.pop("reserveToken", None)
    return item


def unclaimed() -> list[dict]:
    return [_for_browser(i) for i in _read() if _is_free(i)]


def unconfirmed() -> list[dict]:
    """Captured items whose phone confirmation reply has not been sent yet.

    Confirmation is tracked as a durable fact on the item, not as a side
    effect of loop ordering: this covers both items captured this tick and
    any item stranded unconfirmed by an earlier failed tick (e.g. a second
    append in the same batch raised, or a sendMessage reply failed). The
    poller retries every item this returns, every tick, until each is
    marked confirmed - so a capture can never be silently lost from the
    user's point of view, and never confirmed twice either since the
    poller calls `mark_confirmed` only after the reply actually lands.

    A missing `confirmed` key (an item written before the field existed)
    defaults to True, not False: we cannot know it was never confirmed, and
    the safe assumption is that it was, since the alternative is a spurious
    "saved" reply for a message the user already acted on. `append()`
    always stamps `confirmed: False` explicitly, so a genuinely new capture
    is unaffected and still gets exactly one reply. This payload still
    carries `status`, `claim` and `routeHint` (only `reserveToken` is
    stripped) so the poller can rebuild a claimed item's reply without
    re-running the claim.
    """
    return [_for_browser(i) for i in _read() if not i.get("confirmed", True)]


def mark_confirmed(tid: str) -> dict:
    """Stamp confirmed=True on an item, once its reply has actually been sent.

    Flock-guarded like the other mutators, but not gated on current status:
    confirmation is orthogonal to the claim lifecycle (an item can be
    confirmed before, during, or after being claimed), so there is no
    predicate to fail on - only the poller calls this, at most once per
    successful sendMessage.
    """
    return _mutate_if(
        tid,
        lambda i: True,
        lambda i: dict(i, confirmed=True),
        lambda i: "unreachable",
    )


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


def _mutate_if(tid: str, predicate, fn, conflict_msg) -> dict:
    """Mutate item `tid` with `fn` iff `predicate(item)` holds, inside the flock.

    The predicate is checked and the write happens under the same lock
    acquisition, so a concurrent process can never observe (or act on) a
    state between the check and the write. Raises InboxConflict (carrying
    the item's current `.claim`) when the predicate fails, KeyError if the
    tid doesn't exist.
    """
    with _locked():
        items = _read()
        for idx, item in enumerate(items):
            if item["tid"] != tid:
                continue
            if not predicate(item):
                raise InboxConflict(conflict_msg(item), item.get("claim"))
            items[idx] = fn(item)
            _write(items)
            return items[idx]
        raise KeyError(f"no inbox item {tid}")


def reserve(tid: str) -> dict:
    """Atomic CAS: unclaimed -> reserved. First caller wins; the rest raise.

    Stamps a fresh, unguessable `reserveToken` on the item and returns it.
    A caller that later takes over a *stale* reservation (see `_is_free`)
    mints a new token here, which invalidates the stalled holder's copy -
    that holder's `finalize`/`release` will then be refused instead of
    racing the new claimer. See `finalize`/`release`.
    """
    token = secrets.token_hex(8)
    return _mutate_if(
        tid,
        _is_free,
        lambda i: dict(i, status="reserved", reservedAt=time.time(), reserveToken=token),
        lambda i: f"{tid} is already {i.get('status')}",
    )


def _held_by(item: dict, token: str) -> bool:
    return item.get("status") == "reserved" and item.get("reserveToken") == token


def _reserve_conflict_msg(tid: str, token: str):
    def _msg(i: dict) -> str:
        if i.get("status") != "reserved":
            return f"{tid} is not reserved (status={i.get('status')})"
        return f"{tid}'s reservation was taken over by another claimer (stale token)"

    return _msg


def finalize(tid: str, board: str, card_num: int, token: str) -> dict:
    """reserved -> claimed. Raises InboxConflict if not reserved by `token`.

    `token` must be the value `reserve()` returned as `reserveToken` for
    this exact reservation. If the reservation went stale and someone
    else took it over, the item now carries a different token and this
    call is refused - closing the window where a stalled process's
    finalize could otherwise win after another process already claimed
    the item on a different board.
    """
    return _mutate_if(
        tid,
        lambda i: _held_by(i, token),
        lambda i: dict(
            i,
            status="claimed",
            reservedAt=None,
            reserveToken=None,
            claim={"board": board, "cardNum": card_num, "ts": _now()},
        ),
        _reserve_conflict_msg(tid, token),
    )


def release(tid: str, token: str) -> dict:
    """reserved -> unclaimed, for rollback. Raises InboxConflict if not reserved by `token`."""
    return _mutate_if(
        tid,
        lambda i: _held_by(i, token),
        lambda i: dict(i, status="unclaimed", reservedAt=None, reserveToken=None),
        _reserve_conflict_msg(tid, token),
    )


def discard(tid: str) -> dict:
    """unclaimed or reserved -> discarded.

    Raises InboxConflict if the item is already claimed (discarding a
    claimed item would detach a live card's provenance) or already
    discarded. No token is required: discard is a moderation action, not
    a claim-transfer, and it's safe (idempotent-ish) for it to also
    interrupt someone else's in-flight reservation.
    """
    return _mutate_if(
        tid,
        lambda i: i.get("status") in ("unclaimed", "reserved"),
        lambda i: dict(i, status="discarded", reservedAt=None, reserveToken=None),
        lambda i: f"{tid} cannot be discarded (status={i.get('status')})",
    )


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
