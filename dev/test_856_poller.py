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
