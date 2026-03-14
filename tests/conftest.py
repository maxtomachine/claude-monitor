"""Shared fixtures for claude-monitor tests."""

from pathlib import Path

import pytest

from tests.helpers import make_session, make_transcript_jsonl


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
