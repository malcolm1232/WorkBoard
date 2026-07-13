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
