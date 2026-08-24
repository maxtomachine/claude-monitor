"""Shared fixtures for claude-monitor tests."""

from pathlib import Path

import pytest

from tests.helpers import make_session, make_transcript_jsonl


import claude_monitor as _cm

# The REAL locations, captured at import time, before the isolation fixture
# below repoints the module constants at scratch files. Built from the
# app's own resolution (not re-derived here) so the canary can never guard
# a different place than the app writes to.
_REAL_STATE_PATHS = frozenset(
    p.resolve() for p in (
        _cm.PREFS_PATH, _cm.HIDDEN_PATH, _cm.PINNED_PATH,
        _cm.SCAN_CACHE_PATH, _cm.JUMP_REQUEST_PATH, _cm.LAYOUT_PATH,
    )
)


@pytest.fixture(autouse=True)
def _real_state_untouched(monkeypatch):
    """Fail the test that writes to the developer's REAL state files.

    On 2026-08-17 a test that mocked load_prefs() but not save_prefs() let
    on_mount()'s launch-count write clobber the real
    ~/.claude/monitor-prefs.json, wiping saved columns and view state down
    to the test's stub. The isolation fixture below is the cure; this is
    the alarm if the cure ever stops covering a path (a new state file
    added to the app but not to the fixture, a test resolving a path at
    import time, a direct write).

    Attribution is by PROCESS, not by watching the files: a first cut
    hashed the real files before/after each test, which would blame an
    arbitrary test whenever Max's live monitor saved prefs or a
    Ctrl+Shift+N press flipped /tmp/claude-jump-request mid-run, a false
    positive of exactly the load/environment-dependent class this suite
    is trying to be rid of (review, 2026-08-19). Every app write goes
    through Path.write_text on a module constant, so wrapping write_text
    and checking the resolved target against the real locations catches
    precisely this process's writes and nothing else."""
    offenders: list[str] = []
    real_write_text = Path.write_text

    def guarded_write_text(self, *args, **kwargs):
        try:
            if self.resolve() in _REAL_STATE_PATHS:
                offenders.append(str(self))
        except OSError:
            pass
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    yield
    assert not offenders, (
        "test wrote to REAL monitor state (isolation fixture bypassed): "
        + ", ".join(sorted(set(offenders))))


@pytest.fixture(autouse=True)
def _isolate_monitor_state_files(tmp_path, monkeypatch):
    """Every test's on-disk state (prefs, hidden, pinned, scan cache,
    jump-request) points at per-test scratch files, never the real
    ~/.claude/* ones or the shared /tmp/claude-jump-request. A test
    exercising a code path that touches one of these (a jump action
    marking a session seen, launch-count tracking on mount, the jump
    sentinel protocol) without its own explicit mock used to silently
    read and write Max's real files; one such gap wiped his saved
    columns/view_state down to just that test's own stub data
    (2026-08-17). Autouse makes this the default for every test, so the
    failure mode is "this test's scratch file has junk in it" instead of
    "Max's real state got clobbered" or "a real running monitor got a
    stray jump/restart request", regardless of whether any individual
    test remembers its own load_prefs/save_prefs mocks. Any test that
    explicitly monkeypatches one of these itself just overrides this
    default for its own scope, same as any other fixture."""
    import claude_monitor as cm
    monkeypatch.setattr(cm, "PREFS_PATH", tmp_path / "monitor-prefs.json")
    monkeypatch.setattr(cm, "HIDDEN_PATH", tmp_path / "monitor-hidden.json")
    monkeypatch.setattr(cm, "PINNED_PATH", tmp_path / "monitor-pinned.json")
    monkeypatch.setattr(cm, "SCAN_CACHE_PATH", tmp_path / "monitor-scan-cache.json")
    monkeypatch.setattr(cm, "JUMP_REQUEST_PATH", tmp_path / "jump-request")
    monkeypatch.setattr(cm, "LAYOUT_PATH", tmp_path / "monitor-layout.json")


@pytest.fixture
def session():
    """A default working session."""
    return make_session()


@pytest.fixture
def idle_session():
    return make_session(status="idle", last_tool="Read",
                        last_tool_input={"file_path": "/tmp/config.py"})


@pytest.fixture
def tmp_transcript(tmp_path):
    """Write a transcript file and return its path."""
    def _write(content: str | None = None, **kwargs) -> Path:
        p = tmp_path / "test-session.jsonl"
        p.write_text(content or make_transcript_jsonl(**kwargs))
        return p
    return _write
