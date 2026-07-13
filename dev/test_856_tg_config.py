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
    check(cfg.task_column({"columns": []}) == "task", "empty columns list does not raise")
    check(cfg.task_column({}) == "task", "missing columns key does not raise")


def test_blank_alias_never_guesses():
    print("blank alias never resolves, even with exactly one board")
    # The bug only shows up with exactly one resolvable alias: _slug(" ") is
    # "", and "".startswith("") is True for every alias, so a whitespace
    # token would match the user's one and only board.
    write_assignments({"/Users/x/Desktop/WorkBoard/board": 7891})
    cfg.save({"token": "T", "chat_id": 42, "offset": 0})
    check(len(cfg.aliases()) == 1, "single-board fixture has exactly one alias")
    check(cfg.resolve_board(" ") is None, "whitespace alias resolves to None")
    check(cfg.resolve_board("") is None, "empty-string alias resolves to None")
    check(cfg.resolve_board("\t") is None, "tab-only alias resolves to None")


def test_patch_preserves_degraded_config():
    print("_patch preserves fields it doesn't understand / can't validate")

    # (a) full config with custom aliases: set_offset must not drop anything.
    write_assignments({})
    cfg.save({
        "token": "T", "chat_id": 42, "offset": 0,
        "aliases": {"qm": "/Users/x/Desktop/QuantifyMe/HFTAgents/board"},
    })
    cfg.set_offset(5)
    conf = cfg.load()
    check(conf is not None and conf["token"] == "T", "token survives set_offset")
    check(conf is not None and conf["chat_id"] == 42, "chat_id survives set_offset")
    check(conf is not None and conf.get("aliases", {}).get("qm") ==
          "/Users/x/Desktop/QuantifyMe/HFTAgents/board", "custom aliases survive set_offset")
    check(conf is not None and conf["offset"] == 5, "offset itself updated")

    # (b) token but no chat_id: load() sees it as unusable, but set_status
    # must still preserve the token rather than overwrite with just status.
    cfg.config_path().write_text(json.dumps({"token": "T2"}))
    check(cfg.load() is None, "token-without-chat_id is unusable per load()")
    cfg.set_status("x")
    raw = json.loads(cfg.config_path().read_text())
    check(raw.get("token") == "T2", "token survives set_status on an incomplete config")
    check(raw.get("status") == "x", "status still gets applied")

    # (c) corrupt/truncated JSON: set_offset must not raise, and must not
    # fabricate a config that claims to be usable.
    cfg.config_path().write_text("{not valid json")
    try:
        cfg.set_offset(1)
        raised = False
    except Exception:
        raised = True
    check(not raised, "set_offset on corrupt JSON does not raise")
    check(cfg.load() is None, "load() still refuses to call a patched-corrupt config usable")


def test_custom_alias_collision_is_none():
    print("custom alias slug collision resolves to None; same-target collision is fine")
    write_assignments({"/Users/x/Desktop/WorkBoard/board": 7891})
    cfg.save({
        "token": "T", "chat_id": 42, "offset": 0,
        "aliases": {"Trading-Bot": "/Users/x/Desktop/A/board",
                    "TradingBot": "/Users/x/Desktop/B/board"},
    })
    check(cfg.resolve_board("tradingbot") is None,
          "two different custom aliases sharing a slug but different boards resolve to None")
    check(cfg.resolve_board("workboard").name == "board",
          "an unrelated derived alias still resolves fine")

    cfg.save({
        "token": "T", "chat_id": 42, "offset": 0,
        "aliases": {"Trading-Bot": "/Users/x/Desktop/A/board",
                    "TradingBot": "/Users/x/Desktop/A/board"},
    })
    check(str(cfg.resolve_board("tradingbot")).endswith("/A/board"),
          "two custom aliases sharing a slug AND agreeing on the target is not ambiguous")

    cfg.save({
        "token": "T", "chat_id": 42, "offset": 0,
        "aliases": {"WorkBoard": "/Users/x/Desktop/QuantifyMe/HFTAgents/board"},
    })
    check(str(cfg.resolve_board("workboard")).endswith("/HFTAgents/board"),
          "a single custom alias overriding a derived one is a deliberate override, not ambiguity")


def test_non_object_json_is_treated_as_empty():
    print("valid JSON that isn't an object is treated as empty, not a crash")
    write_assignments({})
    cfg.config_path().parent.mkdir(parents=True, exist_ok=True)

    cfg.config_path().write_text(json.dumps([1, 2, 3]))
    check(cfg.load() is None, "bare-list config: load() returns None")
    try:
        cfg.set_offset(1)
        raised = False
    except Exception:
        raised = True
    check(not raised, "_patch on a bare-list config does not raise")

    cfg.config_path().write_text(json.dumps("just a string"))
    check(cfg.load() is None, "bare-string config: load() returns None")
    try:
        cfg.set_status("y")
        raised = False
    except Exception:
        raised = True
    check(not raised, "_patch on a bare-string config does not raise")


def test_unreadable_config_never_clobbered():
    print("an unreadable config file is never clobbered (token survives)")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("  -- skipped: running as root, chmod 000 is still readable")
        return

    write_assignments({})
    cfg.save({
        "token": "REAL-TOKEN", "chat_id": 42, "offset": 0,
        "aliases": {"qm": "/Users/x/Desktop/QuantifyMe/HFTAgents/board"},
    })
    path = cfg.config_path()
    before = path.read_bytes()
    os.chmod(path, 0o000)
    try:
        try:
            cfg.set_offset(5)
            raised_offset = False
        except Exception:
            raised_offset = True
        try:
            cfg.set_status("degraded")
            raised_status = False
        except Exception:
            raised_status = True
        check(not raised_offset, "set_offset on an unreadable config does not raise")
        check(not raised_status, "set_status on an unreadable config does not raise")
        check(cfg.load() is None, "load() reports no usable config while unreadable")
    finally:
        os.chmod(path, 0o600)

    after = path.read_bytes()
    check(after == before, "unreadable config file's bytes are byte-for-byte unchanged")

    conf = cfg.load()
    check(conf is not None and conf.get("token") == "REAL-TOKEN",
          "token survives, once permissions are restored")
    check(conf is not None and conf.get("chat_id") == 42,
          "chat_id survives, once permissions are restored")
    check(conf is not None and conf.get("aliases", {}).get("qm") ==
          "/Users/x/Desktop/QuantifyMe/HFTAgents/board",
          "custom aliases survive, once permissions are restored")


def test_read_raw_survives_unsearchable_parent_dir():
    print("_read_raw / load survive an unsearchable parent directory")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("  -- skipped: running as root, an unsearchable dir is still traversable")
        return

    orig_env = os.environ["BOARD_TELEGRAM_CONFIG"]
    locked_dir = _STATE / "locked_parent"
    locked_dir.mkdir()
    target = locked_dir / "telegram.json"
    target.write_text(json.dumps({"token": "X", "chat_id": 1}))
    os.environ["BOARD_TELEGRAM_CONFIG"] = str(target)
    os.chmod(locked_dir, 0o000)
    try:
        try:
            result = cfg.load()
            raised = False
        except Exception:
            raised = True
        check(not raised, "load() does not crash when the parent dir is unsearchable")
        check(result is None, "load() reports None when the parent dir is unsearchable")
    finally:
        os.chmod(locked_dir, 0o700)
        os.environ["BOARD_TELEGRAM_CONFIG"] = orig_env


def test_stale_tmp_file_not_reused():
    print("a stale, lax-mode tmp file at the old fixed path is not reused")
    write_assignments({})
    stale = cfg.config_path().with_suffix(".tmp")
    cfg.config_path().parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("leftover bytes from a crashed write")
    os.chmod(stale, 0o644)
    try:
        cfg.save({"token": "T3", "chat_id": 7, "offset": 0})
        mode = stat.S_IMODE(cfg.config_path().stat().st_mode)
        check(mode == 0o600, f"final config is still 0600 with a stale tmp file present (got {oct(mode)})")
        check(cfg.load()["token"] == "T3", "save still succeeds with a stale tmp file present")
    finally:
        if stale.exists():
            stale.unlink()


def test_board_dirs_degrades_on_broken_registry():
    print("_board_dirs degrades to [] when the registry is broken")
    import port_registry

    def boom():
        raise RuntimeError("registry exploded")

    orig = port_registry.assignments
    port_registry.assignments = boom
    try:
        check(cfg._board_dirs() == [], "broken registry yields no board dirs, not a crash")
        check(cfg.aliases() == {} or isinstance(cfg.aliases(), dict),
              "aliases() survives a broken registry too")
    finally:
        port_registry.assignments = orig


if __name__ == "__main__":
    test_load_save_perms()
    test_derived_aliases()
    test_custom_alias_wins()
    test_ambiguous_alias_is_none()
    test_task_column()
    test_blank_alias_never_guesses()
    test_patch_preserves_degraded_config()
    test_custom_alias_collision_is_none()
    test_non_object_json_is_treated_as_empty()
    test_unreadable_config_never_clobbered()
    test_read_raw_survives_unsearchable_parent_dir()
    test_stale_tmp_file_not_reused()
    test_board_dirs_degrades_on_broken_registry()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
