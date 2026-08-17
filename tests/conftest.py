"""Shared fixtures for claude-monitor tests."""

from pathlib import Path

import pytest

from tests.helpers import make_session, make_transcript_jsonl


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
