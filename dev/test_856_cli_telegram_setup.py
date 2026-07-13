#!/usr/bin/env python3
"""#856 review IMPORTANT 2 - card.py telegram-setup must never destroy an
existing config (token, chat_id, custom aliases) on a failed run.

Run: python3 dev/test_856_cli_telegram_setup.py
"""
from __future__ import annotations

import argparse
import builtins
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_STATE = Path(tempfile.mkdtemp(prefix="t856tgsetup-"))
os.environ["BOARD_TELEGRAM_CONFIG"] = str(_STATE / "telegram.json")
os.environ["BOARD_ASSIGNMENTS"] = str(_STATE / "assignments.json")
os.environ["BOARD_REGISTRY"] = str(_STATE / "registry.json")
Path(os.environ["BOARD_ASSIGNMENTS"]).write_text("{}")

import _tg_config as cfg  # noqa: E402
import card_commands  # noqa: E402

_fails = 0


def check(cond, msg):
    global _fails
    print(f"  {'OK ' if cond else 'XX '} {msg}")
    if not cond:
        _fails += 1


def fake_input(*answers):
    """Stand-in for builtins.input() so setup never blocks on real stdin."""
    it = iter(answers)

    def _input(prompt=""):
        return next(it, "")

    return _input


def api_no_message_seen(token, method, params, timeout):
    """Telegram accepted the token but the user hasn't messaged the bot yet -
    the single most likely first-run outcome, and the one that used to wipe
    the config (chat_id: 0 saved before this response came back)."""
    assert method == "getUpdates"
    return {"ok": True, "result": []}


def api_rejects_token(token, method, params, timeout):
    return {"ok": False}


def api_network_error(token, method, params, timeout):
    raise TimeoutError("network down")


def api_ok(token, method, params, timeout):
    return {"ok": True, "result": [
        {"update_id": 7, "message": {"chat": {"id": 4242}, "text": "hi"}},
    ]}


def run_setup(token, api):
    """Run cmd_telegram_setup with stdin faked out. Returns the SystemExit
    code, or None if it returned normally."""
    real_input = builtins.input
    builtins.input = fake_input("")  # answers the "press enter" prompt
    try:
        args = argparse.Namespace(token=token)
        try:
            card_commands.cmd_telegram_setup(args, api=api)
            return None
        except SystemExit as e:
            return e.code
    finally:
        builtins.input = real_input


def seed_config():
    cfg.save({
        "token": "OLD-TOKEN", "chat_id": 111, "offset": 3,
        "aliases": {"qm": "/Users/x/Desktop/QuantifyMe/HFTAgents/board",
                    "wb": "/Users/x/Desktop/WorkBoard/board"},
        "status": None,
    })


def test_no_message_seen_leaves_config_untouched():
    print("'no message seen yet' does not wipe an existing config")
    seed_config()
    before = cfg.config_path().read_bytes()

    code = run_setup("111:AAAA-old-token-shape", api_no_message_seen)

    check(code not in (None, 0), "setup exits non-zero on 'no message seen yet'")
    after = cfg.config_path().read_bytes()
    check(after == before, "config file is byte-for-byte unchanged")
    conf = cfg.load()
    check(conf is not None and conf["token"] == "OLD-TOKEN", "old token survives")
    check(conf is not None and conf["chat_id"] == 111, "old chat_id survives (not clobbered to 0)")
    check(conf is not None and conf.get("aliases", {}).get("qm") ==
          "/Users/x/Desktop/QuantifyMe/HFTAgents/board", "custom aliases survive")
    check(conf is not None and conf.get("aliases", {}).get("wb") ==
          "/Users/x/Desktop/WorkBoard/board", "all custom aliases survive")


def test_rejected_token_leaves_config_untouched():
    print("Telegram rejecting the token does not wipe an existing config")
    seed_config()
    before = cfg.config_path().read_bytes()

    code = run_setup("111:BBBB-bad-token-shape", api_rejects_token)

    check(code not in (None, 0), "setup exits non-zero on a rejected token")
    check(cfg.config_path().read_bytes() == before, "config file is byte-for-byte unchanged")


def test_network_error_leaves_config_untouched():
    print("a network error talking to Telegram does not wipe an existing config")
    seed_config()
    before = cfg.config_path().read_bytes()

    code = run_setup("111:CCCC-token-shape", api_network_error)

    check(code not in (None, 0), "setup exits non-zero on a network error")
    check(cfg.config_path().read_bytes() == before, "config file is byte-for-byte unchanged")


def test_malformed_token_leaves_config_untouched():
    print("a token that fails the basic shape check does not wipe an existing config")
    seed_config()
    before = cfg.config_path().read_bytes()

    code = run_setup("not-a-real-token", api_ok)

    check(code not in (None, 0), "setup exits non-zero on a malformed token")
    check(cfg.config_path().read_bytes() == before, "config file is byte-for-byte unchanged")


def test_successful_setup_preserves_aliases_and_writes_once():
    print("a successful run writes exactly once and keeps custom aliases")
    seed_config()

    real_save = cfg.save
    calls = []

    def counting_save(conf):
        calls.append(conf)
        real_save(conf)

    cfg.save = counting_save
    try:
        code = run_setup("111:DDDD-good-token-shape", api_ok)
    finally:
        cfg.save = real_save

    check(code is None, "successful setup returns normally (no sys.exit)")
    check(len(calls) == 1, f"config is written exactly once (got {len(calls)} writes)")
    conf = cfg.load()
    check(conf is not None and conf["token"] == "111:DDDD-good-token-shape", "new token saved")
    check(conf is not None and conf["chat_id"] == 4242, "chat id learned from the response")
    check(conf is not None and conf.get("aliases", {}).get("qm") ==
          "/Users/x/Desktop/QuantifyMe/HFTAgents/board", "existing custom aliases carried over")
    check(conf is not None and conf.get("aliases", {}).get("wb") ==
          "/Users/x/Desktop/WorkBoard/board", "all existing custom aliases carried over")


def test_first_run_no_prior_config_no_message_seen():
    print("a brand-new machine with no config yet: failure leaves it still unconfigured")
    if cfg.config_path().exists():
        cfg.config_path().unlink()
    check(cfg.load() is None, "starts unconfigured")

    code = run_setup("111:EEEE-token-shape", api_no_message_seen)

    check(code not in (None, 0), "setup exits non-zero on 'no message seen yet'")
    check(cfg.load() is None, "still unconfigured, not chat_id=0 (which telegram-alias would "
          "otherwise misreport as 'run telegram-setup first' forever)")
    check(not cfg.config_path().exists(), "no config file was created at all")


if __name__ == "__main__":
    test_no_message_seen_leaves_config_untouched()
    test_rejected_token_leaves_config_untouched()
    test_network_error_leaves_config_untouched()
    test_malformed_token_leaves_config_untouched()
    test_successful_setup_preserves_aliases_and_writes_once()
    test_first_run_no_prior_config_no_message_seen()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
