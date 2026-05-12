"""Invariant tests for cross-source identity coherence.

Each test synthesizes an operation that has historically desynced the five
session-identity sources (hook-state, sessions/{pid}.json, statusline cache,
transcript custom-title, window OSC) and asserts reconcile_sources() catches it.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_monitor as cm
from claude_monitor import reconcile_sources, Discrepancy


@pytest.fixture
def src_dirs(tmp_path):
    """Patch the four source directories to a clean tmp tree."""
    sessions = tmp_path / "sessions"
    states = tmp_path / "session-states"
    cache = tmp_path / "statusline-cache"
    projects = tmp_path / "projects"
    for d in (sessions, states, cache, projects / "proj"):
        d.mkdir(parents=True)
    with patch.object(cm, "SESSIONS_DIR", sessions), \
         patch.object(cm, "HOOK_STATE_DIR", states), \
         patch.object(cm, "_STATUSLINE_CACHE_DIR", cache), \
         patch.object(cm, "CLAUDE_DIR", projects), \
         patch.object(cm, "_hook_state_cache", {}):
        yield {"sessions": sessions, "states": states,
               "cache": cache, "projects": projects / "proj"}


def _kinds(ds: list[Discrepancy]) -> set[str]:
    return {d.kind for d in ds}


def test_clean_session_has_no_discrepancies(src_dirs):
    sid = "aaaa0000-1111-2222-3333-444444444444"
    (src_dirs["sessions"] / "100.json").write_text(
        json.dumps({"pid": 100, "sessionId": sid}))
    (src_dirs["states"] / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "pid": 100, "state": "idle",
                    "title": "myname"}))
    (src_dirs["cache"] / f"claude-name-{sid}").write_text("myname")
    (src_dirs["projects"] / f"{sid}.jsonl").write_text(
        json.dumps({"type": "custom-title", "customTitle": "myname"}) + "\n")
    with patch.object(cm, "_pid_is_claude", return_value=True):
        assert reconcile_sources(sid) == []


def test_detects_pid_moved_after_branch(src_dirs):
    """The /branch case: hook still references PID that now serves a new sid."""
    old, new = ("aaaa0000-0000-0000-0000-000000000000",
                "bbbb0000-0000-0000-0000-000000000000")
    (src_dirs["sessions"] / "4538.json").write_text(
        json.dumps({"pid": 4538, "sessionId": new}))
    (src_dirs["states"] / f"{old}.json").write_text(
        json.dumps({"session_id": old, "pid": 4538, "state": "exited"}))
    with patch.object(cm, "_pid_is_claude", return_value=True):
        ds = reconcile_sources(old)
    assert "pid_mismatch" in _kinds(ds)
    pm = next(d for d in ds if d.kind == "pid_mismatch")
    assert pm.details["pid_now_serves"] == new


def test_detects_subagent_exit_polluted_parent(src_dirs):
    """state=exited but the same PID still serves THIS sid (subagent SessionEnd)."""
    sid = "cccc0000-0000-0000-0000-000000000000"
    (src_dirs["sessions"] / "200.json").write_text(
        json.dumps({"pid": 200, "sessionId": sid}))
    (src_dirs["states"] / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "pid": 200, "state": "exited"}))
    with patch.object(cm, "_pid_is_claude", return_value=True):
        ds = reconcile_sources(sid)
    assert "liveness_mismatch" in _kinds(ds)


def test_detects_multi_pid_same_sid(src_dirs):
    """Two terminals both think they host this conversation (resume-twice)."""
    sid = "dddd0000-0000-0000-0000-000000000000"
    (src_dirs["sessions"] / "300.json").write_text(
        json.dumps({"pid": 300, "sessionId": sid}))
    (src_dirs["sessions"] / "301.json").write_text(
        json.dumps({"pid": 301, "sessionId": sid}))
    with patch.object(cm, "_pid_is_claude", return_value=True):
        ds = reconcile_sources(sid)
    assert "multi_pid_same_sid" in _kinds(ds)
    mp = next(d for d in ds if d.kind == "multi_pid_same_sid")
    assert sorted(mp.details["pids"]) == [300, 301]


def test_detects_name_desync_after_rename(src_dirs):
    """transcript custom-title ≠ statusline-cache name (hook lagged a /rename)."""
    sid = "eeee0000-0000-0000-0000-000000000000"
    (src_dirs["projects"] / f"{sid}.jsonl").write_text(
        json.dumps({"type": "custom-title", "customTitle": "new-name"}) + "\n")
    (src_dirs["cache"] / f"claude-name-{sid}").write_text("old-name")
    (src_dirs["states"] / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "pid": 400, "title": "old-name"}))
    with patch.object(cm, "_pid_is_claude", return_value=True):
        ds = reconcile_sources(sid)
    assert "name_mismatch" in _kinds(ds)


def test_detects_orphan_state(src_dirs):
    """hook-state exists, no PID file claims it, hook PID dead."""
    sid = "ffff0000-0000-0000-0000-000000000000"
    (src_dirs["states"] / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "pid": 99999, "state": "idle"}))
    with patch.object(cm, "_pid_is_claude", return_value=False):
        ds = reconcile_sources(sid)
    assert "orphan_state" in _kinds(ds)


def test_orphan_not_flagged_when_pid_alive(src_dirs):
    sid = "abcd0000-0000-0000-0000-000000000000"
    (src_dirs["states"] / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "pid": 500, "state": "idle"}))
    with patch.object(cm, "_pid_is_claude", return_value=True):
        ds = reconcile_sources(sid)
    assert "orphan_state" not in _kinds(ds)
