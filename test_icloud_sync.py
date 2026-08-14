"""kick_icloud_sync() — the pull that replaced trusting Apple's push.

Background (2026-08-14, ANT-461). macOS launches `voicememod` on demand only:
its LaunchAgent has no RunAtLoad and no KeepAlive, and the only things that
start it are an APNs push and the Voice Memos app. Alex never opens that app on
the Mini, so one missed push stranded every subsequent memo in iCloud with no
retry — two memos sat there for six and one days while this script read the DB
fine and logged "nothing new to process" 235 times.

The invariant that actually matters is #1 below. This helper runs at the top of
every poll, so if it can ever raise, the whole pipeline dies on every run and we
lose *all* memos instead of some — strictly worse than the bug it fixes.

Run:
    .venv/bin/python3.11 -m pytest test_icloud_sync.py -q
"""

import os
import subprocess

import pytest

import process_voice_memos as pvm


def _fn():
    fn = getattr(pvm, "kick_icloud_sync", None)
    assert fn is not None, "process_voice_memos.kick_icloud_sync is missing"
    return fn


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.parametrize(
    "boom",
    [
        OSError("launchctl not found"),
        subprocess.TimeoutExpired(cmd="launchctl", timeout=20),
        RuntimeError("something nobody predicted"),
    ],
    ids=["oserror", "timeout", "unexpected"],
)
def test_a_broken_launchctl_never_takes_down_the_run(monkeypatch, capsys, boom):
    """THE load-bearing test. This runs before every poll; raising here would
    turn a partial outage into a total one."""
    def explode(*a, **kw):
        raise boom

    monkeypatch.setattr(pvm.subprocess, "run", explode)
    assert _fn()() is False           # reports failure...
    assert "WARN" in capsys.readouterr().out   # ...loudly, but does not raise


def test_nonzero_exit_is_reported_not_raised(monkeypatch, capsys):
    monkeypatch.setattr(
        pvm.subprocess, "run",
        lambda *a, **kw: _Result(returncode=3, stderr="Could not find service"),
    )
    assert _fn()() is False
    out = capsys.readouterr().out
    assert "WARN" in out and "3" in out


def test_success_returns_true(monkeypatch):
    monkeypatch.setattr(pvm.subprocess, "run", lambda *a, **kw: _Result(0))
    assert _fn()() is True


def _record(monkeypatch, pgrep_rc=0):
    """Capture every subprocess.run argv. pgrep_rc=0 means voicememod was already
    up; 1 means it was down (the outage case)."""
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return _Result(pgrep_rc if cmd and cmd[0] == "pgrep" else 0)

    monkeypatch.setattr(pvm.subprocess, "run", fake)
    return calls


def _launchctl(calls):
    hits = [c for c in calls if c and c[0] == "launchctl"]
    assert len(hits) == 1, f"expected exactly one launchctl call, got {calls}"
    return hits[0]


def test_targets_this_users_gui_domain(monkeypatch):
    calls = _record(monkeypatch)
    _fn()()
    cmd = _launchctl(calls)
    assert cmd[:2] == ["launchctl", "kickstart"]
    assert cmd[2] == f"gui/{os.getuid()}/com.apple.voicememod"


def test_never_passes_dash_k(monkeypatch):
    """`-k` would kill a possibly mid-download voicememod. Plain kickstart is an
    idempotent no-op when it is already running (verified live: exit 0, same PID),
    which is the whole reason this is safe to call every 30 minutes."""
    calls = _record(monkeypatch)
    _fn()()
    assert "-k" not in _launchctl(calls)


def test_revival_is_logged_but_a_no_op_is_quiet(monkeypatch, capsys):
    """The outage is the event worth seeing. A routine no-op every 30 min is not —
    that is exactly the noise that made 235 identical 'nothing new' lines useless."""
    _record(monkeypatch, pgrep_rc=1)          # voicememod was DOWN
    assert _fn()() is True
    assert "DOWN" in capsys.readouterr().out

    _record(monkeypatch, pgrep_rc=0)          # already running
    assert _fn()() is True
    assert "DOWN" not in capsys.readouterr().out


def test_a_broken_pgrep_does_not_skip_the_kickstart(monkeypatch):
    """The pre-check only feeds a log line. If it fails we still must kick —
    otherwise a broken pgrep silently restores the original bug."""
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if cmd and cmd[0] == "pgrep":
            raise OSError("no pgrep here")
        return _Result(0)

    monkeypatch.setattr(pvm.subprocess, "run", fake)
    assert _fn()() is True
    _launchctl(calls)  # asserts it happened exactly once
