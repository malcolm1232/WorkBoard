#!/usr/bin/env python3
"""#856 - telegram poller.

Run: python3 dev/test_856_poller.py
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
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


def custom_api(resp, sent=None):
    """A stand-in whose getUpdates response is exactly what's handed to it,
    unshaped - used to inject malformed payloads a well-behaved API would
    never normally return."""

    def api(token, method, params, timeout):
        if method == "getUpdates":
            return resp
        if method == "sendMessage":
            if sent is not None:
                sent.append(params["text"])
            return {"ok": True}
        raise AssertionError(f"unexpected method {method}")

    return api


def _fake_script(tmp, name, body):
    p = Path(tmp) / name
    p.write_text(body)
    return p


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


def test_malformed_getupdates_result_shapes():
    print("malformed getUpdates result shapes do not crash the poller")
    for bad_result in (None, "oops", 123, {"not": "a list"}):
        reset()
        new = tp.poll(api=custom_api({"ok": True, "result": bad_result}))
        check(new == [], f"result={bad_result!r} treated as no-op")
        check(cfg.load()["offset"] == 0, f"result={bad_result!r} leaves offset untouched")

    reset()
    new = tp.poll(api=custom_api("not-a-dict-response"))
    check(new == [], "non-dict getUpdates response treated as no-op")

    reset()
    new = tp.poll(api=custom_api(None))
    check(new == [], "None getUpdates response treated as no-op")

    reset()
    new = tp.poll(api=custom_api([1, 2, 3]))
    check(new == [], "a list (instead of a dict) getUpdates response treated as no-op")


def test_malformed_update_entries_skipped():
    print("malformed individual update entries are skipped, not fatal")
    reset()
    bad_entries = [
        "not-a-dict",
        123,
        {"update_id": 1},                            # no message/channel_post
        {"update_id": 2, "message": "not-a-dict"},    # message wrong type
        {"update_id": 3, "message": None},            # message explicitly null
        {"update_id": 4, "channel_post": ["nope"]},   # channel_post wrong type
    ]
    good = upd(5, "the real one")
    new = tp.poll(api=custom_api({"ok": True, "result": bad_entries + [good]}))
    check(len(new) == 1 and new[0]["title"] == "the real one",
          "only the well-formed update is captured")
    check(cfg.load()["offset"] == 6,
          "offset advances past the highest update_id seen, malformed or not")


def test_stranded_capture_gets_confirmed_next_tick():
    print("reviewer repro: append raises on the 2nd update in a batch")
    reset()
    real_append = _inbox.append
    calls = {"n": 0}

    def flaky_append(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_append(*a, **k)

    _inbox.append = flaky_append
    sent = []
    try:
        try:
            tp.poll(api=fake_api([upd(70, "first"), upd(71, "second")], sent))
            check(False, "poll must propagate the append failure")
        except OSError:
            check(True, "poll propagates the OSError instead of swallowing it")
    finally:
        _inbox.append = real_append

    check(cfg.load()["offset"] == 0, "offset did not advance past either update")
    stranded = _inbox.unclaimed()
    check(len(stranded) == 1 and stranded[0]["title"] == "first",
          "the first item is safely in the inbox")
    check(sent == [], "no confirmation was sent this tick (it raised before the confirm phase)")
    check(_inbox.get(stranded[0]["tid"])["confirmed"] is False,
          "the stranded item is not confirmed yet")

    sent2 = []
    new2 = tp.poll(api=fake_api([upd(70, "first"), upd(71, "second")], sent2))
    check(len(new2) == 1 and new2[0]["title"] == "second",
          "the second update is finally captured on the next tick")
    check(len(sent2) == 2, f"exactly two confirmations sent this tick (got {sent2})")
    both = _inbox.unclaimed()
    check(len(both) == 2, "both items now in the inbox")
    check(all(_inbox.get(i["tid"])["confirmed"] for i in both),
          "both items are now confirmed - the first exactly once across the two ticks")


def test_claim_hint_subprocess_behavior():
    print("_claim_hint: real subprocess construction, timeout, returncode, stdout parsing")
    tmp = tempfile.mkdtemp(prefix="t856claim-")
    real_card_py = tp.CARD_PY
    real_timeout = tp._CLAIM_TIMEOUT
    real_resolve = cfg.resolve_board
    cfg.resolve_board = lambda alias: Path(tmp)  # any existing dir; the fake script ignores it
    item = {"tid": "T-1", "routeHint": "qm"}
    try:
        # Successful claim: stdout matches card.py's real "+ #<num> ..." shape.
        tp.CARD_PY = _fake_script(tmp, "ok.py", "print('+ #431 some idea -> task')\n")
        check(tp._claim_hint(item) == "saved -> qm #431", "successful claim returns the card number")

        # A title that itself contains a hash-number must not confuse the parse:
        # the real number is anchored to the start of the "+ #N" line.
        tp.CARD_PY = _fake_script(
            tmp, "hashy.py",
            "print('+ #431 remember to file #12 as a followup -> task')\n",
        )
        check(tp._claim_hint(item) == "saved -> qm #431",
              "a '#12' inside the title does not shadow the real card number")

        # Non-zero exit: card.py itself failed (e.g. no such tid) - caller falls
        # back to a plain 'saved' and the item stays in the inbox.
        tp.CARD_PY = _fake_script(tmp, "fail.py", "import sys\nprint('boom')\nsys.exit(1)\n")
        check(tp._claim_hint(item) is None, "non-zero exit returns None")

        # Garbage stdout on a clean exit must not raise, even without a match.
        tp.CARD_PY = _fake_script(tmp, "garbage.py", "print('nothing card-shaped here')\n")
        check(tp._claim_hint(item) == "saved", "unparseable stdout does not raise (falls back to 'saved')")

        # A hang must be cut off by the timeout rather than blocking the
        # scheduled run forever.
        tp.CARD_PY = _fake_script(
            tmp, "hang.py",
            "import time\ntime.sleep(5)\nprint('+ #999 too late')\n",
        )
        tp._CLAIM_TIMEOUT = 0.3
        started = time.time()
        result = tp._claim_hint(item)
        elapsed = time.time() - started
        check(result is None, "a hang returns None instead of blocking forever")
        check(elapsed < 4.0, f"the timeout actually cut the subprocess off (took {elapsed:.2f}s)")
    finally:
        tp.CARD_PY = real_card_py
        tp._CLAIM_TIMEOUT = real_timeout
        cfg.resolve_board = real_resolve


def test_second_concurrent_poller_noops():
    print("single-instance guard: a second concurrent poller no-ops instead of racing")
    reset()
    lp = tp._lock_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    holder = lp.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # simulate a poller already in flight
    try:
        new = tp.poll(api=fake_api([upd(80, "should not be captured")]))
        check(new == [], "the locked-out poller returns [] instead of racing")
        check(_inbox.unclaimed() == [], "nothing captured while another poller holds the lock")
        check(cfg.load()["offset"] == 0, "offset untouched by the locked-out poller")
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    # Once the lock is released, a poll proceeds normally.
    new2 = tp.poll(api=fake_api([upd(80, "now it goes through")]))
    check(len(new2) == 1, "a poll after the lock is released captures normally")


def test_http_429_and_500_are_silent():
    print("HTTP 429 and 500 degrade silently, no status set")
    for code, reason in ((429, "Too Many Requests"), (500, "Internal Server Error")):
        reset()
        err = urllib.error.HTTPError("u", code, reason, {}, None)
        new = tp.poll(api=fake_api([], fail=err))
        check(new == [], f"{code} returns no items")
        check(cfg.load()["offset"] == 0, f"{code} leaves the offset untouched")
        check(not cfg.load().get("status"), f"{code} does not set the token-invalid status")


def test_sendmessage_failure_leaves_capture_unconfirmed_for_retry():
    print("sendMessage raising: capture survives and stays unconfirmed for retry")
    reset()

    def flaky_sendmessage_api(token, method, params, timeout):
        if method == "getUpdates":
            return {"ok": True, "result": [upd(90, "resilient")]}
        if method == "sendMessage":
            raise urllib.error.URLError("down")
        raise AssertionError(method)

    new = tp.poll(api=flaky_sendmessage_api)
    check(len(new) == 1, "the capture lands even though the reply failed")
    item = _inbox.get(new[0]["tid"])
    check(item is not None, "the item is present in the inbox")
    check(item.get("confirmed") is False, "the item stays unconfirmed after the failed reply")
    check(cfg.load()["offset"] == 91, "offset still advances: the capture itself succeeded")

    sent = []
    new2 = tp.poll(api=fake_api([upd(90, "resilient")], sent))
    check(new2 == [], "no duplicate capture on retry (update_id dedupe)")
    check(len(sent) == 1, f"the stranded item finally gets its confirmation on retry ({sent})")
    check(_inbox.get(item["tid"])["confirmed"] is True, "the item is now marked confirmed")


def test_legacy_item_gets_no_confirmation_reply():
    print("legacy item with no confirmed key gets no reply from a poll")
    reset()
    legacy = {
        "tid": "T-1",
        "update_id": 995,
        "text": "legacy capture",
        "title": "legacy capture",
        "url": "",
        "routeHint": None,
        "ts": "2020-01-01T00:00:00Z",
        "status": "unclaimed",
        "claim": None,
        "reserveToken": None,
        # NOTE: no "confirmed" key - written before the field existed.
    }
    _inbox.path().parent.mkdir(parents=True, exist_ok=True)
    with _inbox.path().open("a") as fh:
        fh.write(json.dumps(legacy) + "\n")

    sent = []
    new = tp.poll(api=fake_api([], sent))
    check(new == [], "no new capture this tick")
    check(sent == [], f"no confirmation reply sent for the legacy item ({sent})")
    check(_inbox.get(legacy["tid"])["status"] == "unclaimed", "the legacy item itself is untouched")


def test_mark_confirmed_failure_does_not_crash_poll():
    print("mark_confirmed raising: poll degrades instead of crashing, capture survives")
    reset()
    sent = []

    def boom(tid):
        raise OSError("disk full")

    real_mark = _inbox.mark_confirmed
    _inbox.mark_confirmed = boom
    try:
        new = tp.poll(api=fake_api([upd(96, "resilient-2")], sent))
    finally:
        _inbox.mark_confirmed = real_mark

    check(len(new) == 1, "the capture still lands even though mark_confirmed failed")
    check(len(sent) == 1, f"the confirmation reply was still sent ({sent})")
    item = _inbox.get(new[0]["tid"])
    check(item is not None, "the item survives in the inbox")
    check(item.get("confirmed") is False,
          "confirmed stays False on disk since the mark_confirmed write itself failed")


def test_retry_after_claim_success_send_failure_keeps_card_number():
    print("retry: claim succeeded but the reply failed to send - retry must not lose the card number")
    reset()
    claim_calls = []

    def fake_claim(item):
        claim_calls.append(item["tid"])
        current = _inbox.get(item["tid"])
        if current and current.get("status") != "claimed":
            reserved = _inbox.reserve(item["tid"])
            _inbox.finalize(item["tid"], board="/tmp/qm-board", card_num=555,
                             token=reserved["reserveToken"])
        return "saved -> qm #555"

    real_claim_hint = tp._claim_hint
    tp._claim_hint = fake_claim

    def flaky_sendmessage_api(token, method, params, timeout):
        if method == "getUpdates":
            return {"ok": True, "result": [upd(97, "#qm needs routing")]}
        if method == "sendMessage":
            raise urllib.error.URLError("down")
        raise AssertionError(method)

    try:
        new = tp.poll(api=flaky_sendmessage_api)
    finally:
        tp._claim_hint = real_claim_hint

    check(len(new) == 1, "the capture lands")
    item = _inbox.get(new[0]["tid"])
    check(item["status"] == "claimed", "the item got claimed even though the reply failed")
    check(item["confirmed"] is False, "still unconfirmed since the reply never sent")
    check(claim_calls == [item["tid"]], "the claim path ran exactly once, on the failed-reply tick")

    # Retry tick: sendMessage now works. _claim_hint must NOT be invoked
    # again for this already-claimed item - the reply must be rebuilt from
    # the claim already recorded on the item, not re-derived by re-running
    # the claim (which card.py would now refuse and degrade to bare "saved").
    tp._claim_hint = fake_claim
    sent = []
    try:
        new2 = tp.poll(api=fake_api([upd(97, "#qm needs routing")], sent))
    finally:
        tp._claim_hint = real_claim_hint

    check(new2 == [], "no duplicate capture on retry (update_id dedupe)")
    check(claim_calls == [item["tid"]], "_claim_hint was not called again on the retry")
    check(sent == ["saved -> qm #555"], f"retry reply rebuilt from the recorded claim ({sent})")
    check(_inbox.get(item["tid"])["confirmed"] is True, "the item is now confirmed")


if __name__ == "__main__":
    test_captures_and_confirms()
    test_ignores_other_chats()
    test_ignores_commands()
    test_dedupe_on_redelivery()
    test_network_failure_is_a_noop()
    test_bad_token_sets_status()
    test_offset_not_advanced_if_inbox_write_fails()
    test_route_hint_claims_and_echoes()
    test_malformed_getupdates_result_shapes()
    test_malformed_update_entries_skipped()
    test_stranded_capture_gets_confirmed_next_tick()
    test_claim_hint_subprocess_behavior()
    test_second_concurrent_poller_noops()
    test_http_429_and_500_are_silent()
    test_sendmessage_failure_leaves_capture_unconfirmed_for_retry()
    test_legacy_item_gets_no_confirmation_reply()
    test_mark_confirmed_failure_does_not_crash_poll()
    test_retry_after_claim_success_send_failure_keeps_card_number()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
