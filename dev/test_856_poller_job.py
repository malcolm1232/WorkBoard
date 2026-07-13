#!/usr/bin/env python3
"""#856 - the 15-minute poller job.

Run: python3 dev/test_856_poller_job.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import install_autostart as ia  # noqa: E402
import install_launchd as il  # noqa: E402
import install_systemd as isd  # noqa: E402

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


class _FakeCompletedProcess:
    """Stand-in for subprocess.CompletedProcess - just enough attrs to be safe
    to ignore, since neither install_systemd.install_poller nor
    uninstall_poller inspect the return value."""
    returncode = 0
    stdout = ""
    stderr = ""


def _stub_systemctl(monkeypatch_calls):
    """Returns a fake subprocess.run that records every argv it was called
    with (so a test can assert ONLY systemctl was ever invoked - never a real
    launchctl/systemctl call escapes to the real machine) and always
    succeeds without touching anything."""
    def _fake_run(argv, *a, **k):
        monkeypatch_calls.append(list(argv))
        return _FakeCompletedProcess()
    return _fake_run


def test_systemd_poller_unit_content():
    print("systemd poller units (#856 review IMPORTANT 2)")
    fake_dir = Path(tempfile.mkdtemp(prefix="t856systemd-"))
    calls: list[list[str]] = []
    orig_run = isd.subprocess.run
    isd.subprocess.run = _stub_systemctl(calls)
    try:
        isd.install_poller(unit_dir=fake_dir)
    finally:
        isd.subprocess.run = orig_run

    service = (fake_dir / "boardsteward-telegram.service").read_text()
    timer = (fake_dir / "boardsteward-telegram.timer").read_text()
    check("Type=oneshot" in service, "service unit is a oneshot (runs once per tick, doesn't stay resident)")
    check("telegram_poller.py" in service, "service unit's ExecStart points at telegram_poller.py")
    check("OnUnitActiveSec=15min" in timer, "timer fires every 15 minutes")
    check("Persistent=true" in timer, "timer catches up a missed run (machine was asleep) - Persistent=true")
    check(bool(calls) and all(c[0] == "systemctl" for c in calls),
          "only systemctl was invoked - never launchctl, never the real one without stubbing")
    check(not str(il.PLIST_DIR).startswith(str(fake_dir)) and fake_dir != isd.UNIT_DIR,
          "sanity: the fake dir used for this test is not the user's real systemd unit dir")


def test_systemd_poller_uninstall():
    print("systemd poller uninstall (#856 review MINOR 4)")
    fake_dir = Path(tempfile.mkdtemp(prefix="t856systemd-un-"))
    calls: list[list[str]] = []
    orig_run = isd.subprocess.run
    isd.subprocess.run = _stub_systemctl(calls)
    try:
        isd.install_poller(unit_dir=fake_dir)
        check((fake_dir / "boardsteward-telegram.service").exists(), "service unit written before uninstall")
        isd.uninstall_poller(unit_dir=fake_dir)
    finally:
        isd.subprocess.run = orig_run
    check(not (fake_dir / "boardsteward-telegram.service").exists(), "service unit removed")
    check(not (fake_dir / "boardsteward-telegram.timer").exists(), "timer unit removed")
    check(all(c[0] == "systemctl" for c in calls), "uninstall only ever shells out to systemctl")


def _swap(obj, name, value):
    """Return (old_value) after setting obj.name = value, for manual monkeypatching
    without pytest's fixture (this suite is plain-python, run via `python3 file.py`)."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    return old


def test_autostart_dispatch_darwin_install():
    print("autostart dispatch: darwin -> launchd (install)")
    orig_platform = sys.platform
    called = []
    orig_fn = _swap(il, "install_poller", lambda *a, **k: called.append("launchd-install"))
    sys.platform = "darwin"
    try:
        ia.install_poller()
    finally:
        sys.platform = orig_platform
        il.install_poller = orig_fn
    check(called == ["launchd-install"], "darwin routes install_poller() to install_launchd, not systemd")


def test_autostart_dispatch_linux_install():
    print("autostart dispatch: linux -> systemd (install)")
    orig_platform = sys.platform
    called = []
    orig_fn = _swap(isd, "install_poller", lambda *a, **k: called.append("systemd-install"))
    sys.platform = "linux"
    try:
        ia.install_poller()
    finally:
        sys.platform = orig_platform
        isd.install_poller = orig_fn
    check(called == ["systemd-install"], "linux routes install_poller() to install_systemd, not launchd")


def test_autostart_dispatch_other_install_raises():
    print("autostart dispatch: unsupported platform (install)")
    orig_platform = sys.platform
    sys.platform = "freebsd13"
    msg = ""
    ok = False
    try:
        try:
            ia.install_poller()
        except RuntimeError as e:
            ok = True
            msg = str(e)
    finally:
        sys.platform = orig_platform
    check(ok, "unsupported platform raises RuntimeError instead of silently doing nothing")
    check("telegram_poller.py" in msg, f"the error names the manual command to run instead ({msg!r})")


def test_autostart_dispatch_darwin_uninstall():
    print("autostart dispatch: darwin -> launchd (uninstall)")
    orig_platform = sys.platform
    called = []
    orig_fn = _swap(il, "uninstall_poller", lambda *a, **k: called.append("launchd-uninstall"))
    sys.platform = "darwin"
    try:
        ia.uninstall_poller()
    finally:
        sys.platform = orig_platform
        il.uninstall_poller = orig_fn
    check(called == ["launchd-uninstall"], "darwin routes uninstall_poller() to install_launchd")


def test_autostart_dispatch_linux_uninstall():
    print("autostart dispatch: linux -> systemd (uninstall)")
    orig_platform = sys.platform
    called = []
    orig_fn = _swap(isd, "uninstall_poller", lambda *a, **k: called.append("systemd-uninstall"))
    sys.platform = "linux"
    try:
        ia.uninstall_poller()
    finally:
        sys.platform = orig_platform
        isd.uninstall_poller = orig_fn
    check(called == ["systemd-uninstall"], "linux routes uninstall_poller() to install_systemd")


def test_autostart_dispatch_other_uninstall_raises():
    print("autostart dispatch: unsupported platform (uninstall)")
    orig_platform = sys.platform
    sys.platform = "freebsd13"
    msg = ""
    ok = False
    try:
        try:
            ia.uninstall_poller()
        except RuntimeError as e:
            ok = True
            msg = str(e)
    finally:
        sys.platform = orig_platform
    check(ok, "unsupported platform raises RuntimeError on uninstall too")
    check("telegram_poller.py" in msg, f"the error names the manual job to remove instead ({msg!r})")


if __name__ == "__main__":
    test_plist_shape()
    test_install_is_idempotent_dry_run()
    test_systemd_poller_unit_content()
    test_systemd_poller_uninstall()
    test_autostart_dispatch_darwin_install()
    test_autostart_dispatch_linux_install()
    test_autostart_dispatch_other_install_raises()
    test_autostart_dispatch_darwin_uninstall()
    test_autostart_dispatch_linux_uninstall()
    test_autostart_dispatch_other_uninstall_raises()
    print("PASS" if _fails == 0 else f"FAIL ({_fails})")
    sys.exit(1 if _fails else 0)
