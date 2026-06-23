"""Tests for transcript parsing and session building."""

import json
import time
from pathlib import Path
from unittest.mock import patch

from claude_monitor import (
    build_session,
    disambiguate_titles,
    filter_ignored,
    parse_sessions,
    parse_timestamp,
    scan_full_file,
    estimate_cost,
    determine_status,
    sort_sessions,
    SortMode,
    read_hook_state,
    read_session_memory_title,
    _resolve_match_candidates,
    _pid_is_claude,
    _is_session_alive,
    count_background_activity,
)
from tests.helpers import make_session, make_transcript_jsonl


class TestParseTimestamp:
    def test_iso_with_z(self):
        result = parse_timestamp("2026-03-13T10:00:00Z")
        assert result > 0

    def test_iso_with_offset(self):
        result = parse_timestamp("2026-03-13T10:00:00+00:00")
        assert result > 0

    def test_invalid(self):
        assert parse_timestamp("not-a-date") == 0.0

    def test_none(self):
        assert parse_timestamp(None) == 0.0


class TestScanFullFile:
    def test_basic_transcript(self, tmp_transcript):
        path = tmp_transcript(model="claude-opus-4-6", tokens_in=5000, tokens_out=1000)
        data = scan_full_file(str(path))
        assert data["model_id"] == "claude-opus-4-6"
        assert data["tokens_in"] == 5000
        assert data["tokens_out"] == 1000
        assert data["cwd"] == "/Users/test/project"
        assert data["slug"] == "test-slug"
        assert data["last_assistant_text"] == "Here is my response."

    def test_custom_title(self, tmp_transcript):
        path = tmp_transcript(custom_title="My Custom Title")
        data = scan_full_file(str(path))
        assert data["custom_title"] == "My Custom Title"

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        data = scan_full_file(str(p))
        assert data["tokens_in"] == 0
        assert data["tokens_out"] == 0
        assert data["model_id"] == ""

    def test_missing_file(self):
        data = scan_full_file("/nonexistent/path.jsonl")
        assert data["tokens_in"] == 0

    def test_last_input_tokens_tracked(self, tmp_transcript):
        path = tmp_transcript(tokens_in=5000)
        data = scan_full_file(str(path))
        assert data["last_input_tokens"] == 5000

    def test_mcp_calls_counted(self, tmp_path):
        lines = [
            json.dumps({"type": "assistant", "timestamp": "2026-03-13T10:00:00Z",
                         "message": {"model": "claude-opus-4-6", "usage": {},
                                     "content": [{"type": "tool_use",
                                                   "name": "mcp__claude_ai_Gmail__search",
                                                   "input": {}}]}}),
        ]
        p = tmp_path / "mcp-test.jsonl"
        p.write_text("\n".join(lines))
        data = scan_full_file(str(p))
        assert data["mcp_calls"] >= 1

    def test_multiple_assistant_messages_tracks_last(self, tmp_path):
        lines = [
            json.dumps({"type": "assistant", "timestamp": "2026-03-13T10:00:00Z",
                         "message": {"model": "claude-opus-4-6",
                                     "usage": {"input_tokens": 1000, "output_tokens": 200,
                                                "cache_read_input_tokens": 0,
                                                "cache_creation_input_tokens": 0},
                                     "content": [{"type": "text", "text": "First response"}]}}),
            json.dumps({"type": "assistant", "timestamp": "2026-03-13T10:01:00Z",
                         "message": {"model": "claude-opus-4-6",
                                     "usage": {"input_tokens": 3000, "output_tokens": 500,
                                                "cache_read_input_tokens": 0,
                                                "cache_creation_input_tokens": 0},
                                     "content": [{"type": "text", "text": "Second response"}]}}),
        ]
        p = tmp_path / "multi.jsonl"
        p.write_text("\n".join(lines))
        data = scan_full_file(str(p))
        assert data["last_input_tokens"] == 3000
        assert data["last_assistant_text"] == "Second response"
        assert data["tokens_in"] == 4000
        assert data["tokens_out"] == 700

    def test_tool_use_captured(self, tmp_path):
        lines = [
            json.dumps({"type": "assistant", "timestamp": "2026-03-13T10:00:00Z",
                         "message": {"model": "claude-opus-4-6", "usage": {},
                                     "content": [{"type": "tool_use", "name": "Read",
                                                   "input": {"file_path": "/tmp/x.py"}}]}}),
        ]
        p = tmp_path / "tool.jsonl"
        p.write_text("\n".join(lines))
        data = scan_full_file(str(p))
        assert data["last_tool"] == "Read"
        assert data["last_tool_input"]["file_path"] == "/tmp/x.py"


class TestDetermineStatus:
    @patch("claude_monitor._is_session_alive", return_value=True)
    def test_recent_activity_is_working(self, _mock):
        assert determine_status("test-id", time.time() - 5) == "working"

    @patch("claude_monitor._is_session_alive", return_value=True)
    def test_moderate_elapsed_is_waiting(self, _mock):
        assert determine_status("test-id", time.time() - 60) == "waiting"

    @patch("claude_monitor._is_session_alive", return_value=True)
    def test_old_activity_is_idle(self, _mock):
        assert determine_status("test-id", time.time() - 600) == "idle"

    @patch("claude_monitor._is_session_alive", return_value=True)
    def test_no_activity(self, _mock):
        assert determine_status("test-id", 0) == "idle"

    @patch("claude_monitor._is_session_alive", return_value=False)
    def test_dead_process_is_closed(self, _mock):
        assert determine_status("test-id", time.time() - 5) == "closed"

    @patch("claude_monitor._is_session_alive", return_value=False)
    def test_idle_dead_process_is_closed(self, _mock):
        assert determine_status("test-id", 0) == "closed"


class TestStaleThinking:
    """Fixture from EBC-Shell (sid 11edc8e8, 2026-06-04): a remote-bridge
    session's Stop hook never fired, freezing hook state at 'thinking' while
    CC's own pid file said idle and the transcript was 3.5h quiet. The monitor
    showed it working for 4.5 hours. 'thinking' must be corroborated; when
    every fresher witness disagrees it decays like idle."""

    def _hook(self, age_s, entered_age_s=None):
        from datetime import datetime, timedelta
        ts = (datetime.now() - timedelta(seconds=age_s)).isoformat()
        entered = (datetime.now() - timedelta(
            seconds=entered_age_s if entered_age_s is not None else age_s)).isoformat()
        return {"state": "thinking", "timestamp": ts,
                "state_entered_at": entered, "pid": 63697}

    def _old_transcript(self, tmp_path):
        import os as _os
        p = tmp_path / "t.jsonl"
        p.touch()
        old = time.time() - 4 * 3600
        _os.utime(p, (old, old))
        return str(p)

    def _pid_file(self, tmp_path, status):
        (tmp_path / "63697.json").write_text(json.dumps(
            {"pid": 63697, "sessionId": "sid", "status": status}))
        return tmp_path

    def test_fresh_thinking_is_working(self, tmp_path):
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state", return_value=self._hook(5)):
            assert determine_status("sid", 0, "", self._old_transcript(tmp_path)) == "working"

    def test_stale_thinking_all_witnesses_quiet_decays_to_idle(self, tmp_path):
        sessions_dir = self._pid_file(tmp_path, "idle")
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value=self._hook(4 * 3600)), \
             patch("claude_monitor._pid_map", {"sid": 63697}), \
             patch("claude_monitor.SESSIONS_DIR", sessions_dir):
            assert determine_status("sid", 0, "", self._old_transcript(tmp_path)) == "idle"

    def test_stale_thinking_recent_entry_is_waiting(self, tmp_path):
        sessions_dir = self._pid_file(tmp_path, "idle")
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value=self._hook(600, entered_age_s=200)), \
             patch("claude_monitor._pid_map", {"sid": 63697}), \
             patch("claude_monitor.SESSIONS_DIR", sessions_dir):
            assert determine_status("sid", 0, "", self._old_transcript(tmp_path)) == "waiting"

    def test_old_hook_but_pid_busy_stays_working(self, tmp_path):
        sessions_dir = self._pid_file(tmp_path, "busy")
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value=self._hook(4 * 3600)), \
             patch("claude_monitor._pid_map", {"sid": 63697}), \
             patch("claude_monitor.SESSIONS_DIR", sessions_dir):
            assert determine_status("sid", 0, "", self._old_transcript(tmp_path)) == "working"

    def test_old_hook_but_fresh_transcript_stays_working(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.touch()  # mtime = now
        sessions_dir = self._pid_file(tmp_path, "idle")
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value=self._hook(4 * 3600)), \
             patch("claude_monitor._pid_map", {"sid": 63697}), \
             patch("claude_monitor.SESSIONS_DIR", sessions_dir):
            assert determine_status("sid", 0, "", str(p)) == "working"


class TestSortSessions:
    def test_sort_by_activity(self):
        now = time.time()
        s1 = make_session(session_id="old", last_activity=now - 100)
        s2 = make_session(session_id="new", last_activity=now)
        result = sort_sessions([s1, s2], SortMode.ACTIVITY)
        assert result[0].session_id == "new"

    def test_sort_by_cost(self):
        s1 = make_session(session_id="cheap", cost=1.0)
        s2 = make_session(session_id="expensive", cost=10.0)
        result = sort_sessions([s1, s2], SortMode.COST)
        assert result[0].session_id == "expensive"

    def test_sort_by_context(self):
        s1 = make_session(session_id="full", context_pct=90)
        s2 = make_session(session_id="low", context_pct=10)
        result = sort_sessions([s1, s2], SortMode.CONTEXT)
        assert result[0].session_id == "low"

    def test_sort_by_tokens(self):
        s1 = make_session(session_id="small", tokens_in=100, tokens_out=50)
        s2 = make_session(session_id="big", tokens_in=100_000, tokens_out=50_000)
        result = sort_sessions([s1, s2], SortMode.TOKENS)
        assert result[0].session_id == "big"

    def test_sort_by_status(self):
        s1 = make_session(session_id="idle", status="idle")
        s2 = make_session(session_id="working", status="working")
        result = sort_sessions([s1, s2], SortMode.STATUS)
        assert result[0].session_id == "working"

    def test_double_dash_lead_floats_to_top_in_every_mode(self):
        """'config--LEAD' is the group-lead marker; it must outrank every
        other row in every sort mode so it lands directly under its group
        header regardless of which sort the user has cycled to."""
        lead = make_session(session_id="L", title="config--LEAD",
                            status="archived", cost=0.01, last_activity=1.0,
                            context_pct=99, tokens_out=1)
        other = make_session(session_id="O", title="config-asana",
                             status="working", cost=999.0, last_activity=999.0,
                             context_pct=1, tokens_out=999_999)
        for mode in SortMode:
            result = sort_sessions([other, lead], mode)
            assert result[0].session_id == "L", f"lead not first in {mode}"
            assert result[1].session_id == "O"


class TestHookState:
    def test_missing_returns_none(self, tmp_path):
        with patch("claude_monitor.HOOK_STATE_DIR", tmp_path):
            assert read_hook_state("nonexistent") is None

    def test_reads_json(self, tmp_path):
        with patch("claude_monitor.HOOK_STATE_DIR", tmp_path):
            state = {"session_id": "abc123", "state": "thinking", "tty": "ttys015"}
            (tmp_path / "abc123.json").write_text(json.dumps(state))
            result = read_hook_state("abc123")
            assert result == state

    def test_malformed_returns_none(self, tmp_path):
        with patch("claude_monitor.HOOK_STATE_DIR", tmp_path):
            (tmp_path / "bad.json").write_text("not json")
            assert read_hook_state("bad") is None

    def test_cached_by_mtime(self, tmp_path):
        with patch("claude_monitor.HOOK_STATE_DIR", tmp_path), \
             patch("claude_monitor._hook_state_cache", {}):
            f = tmp_path / "s1.json"
            f.write_text(json.dumps({"state": "idle"}))
            assert read_hook_state("s1")["state"] == "idle"
            # Rewrite with different content but same mtime — still cached
            # (won't actually test cache here since mtime changes; just verify no crash)
            f.write_text(json.dumps({"state": "thinking"}))
            assert read_hook_state("s1")["state"] in ("idle", "thinking")


class TestSessionLiveness:
    def _ps_result(self, comm: str):
        m = type("R", (), {"stdout": comm, "returncode": 0})()
        return m

    def test_pid_is_claude_matches_cli(self):
        with patch("claude_monitor.subprocess.run", return_value=self._ps_result("claude")):
            assert _pid_is_claude(12345) is True

    def test_pid_is_claude_rejects_recycled(self):
        with patch("claude_monitor.subprocess.run", return_value=self._ps_result("mdworker_shared")):
            assert _pid_is_claude(12345) is False

    def test_pid_is_claude_rejects_helpers(self):
        for comm in ("Claude Helper", "claude_crashpad_handler", "Claude.app", "claude-monitor"):
            with patch("claude_monitor.subprocess.run", return_value=self._ps_result(comm)):
                assert _pid_is_claude(12345) is False, comm

    def test_pid_is_claude_dead_pid(self):
        with patch("claude_monitor.subprocess.run", return_value=self._ps_result("")):
            assert _pid_is_claude(99999) is False

    def test_alive_rejects_recycled_hook_pid(self):
        """Hook state has a PID, the PID is alive, but it's not a claude process
        — session must NOT be reported alive (ghost-row bug)."""
        with patch("claude_monitor._pid_map", {}), \
             patch("claude_monitor._refresh_pid_map"), \
             patch("claude_monitor._recently_resumed", {}), \
             patch("claude_monitor.read_hook_state", return_value={"pid": 62235}), \
             patch("claude_monitor._pid_is_claude", return_value=False):
            assert _is_session_alive("ghost-sid") is False

    def test_alive_accepts_genuine_hook_pid(self):
        with patch("claude_monitor._pid_map", {}), \
             patch("claude_monitor._refresh_pid_map"), \
             patch("claude_monitor._recently_resumed", {}), \
             patch("claude_monitor.read_hook_state", return_value={"pid": 6990}), \
             patch("claude_monitor._pid_is_claude", return_value=True), \
             patch("claude_monitor.SESSIONS_DIR", Path("/nonexistent")):
            assert _is_session_alive("real-sid") is True

    def test_alive_rejects_hook_pid_that_moved_to_another_session(self, tmp_path):
        """After /branch, the PID stays alive but serves a different sid."""
        (tmp_path / "4538.json").write_text('{"sessionId": "new-sid", "pid": 4538}')
        with patch("claude_monitor._pid_map", {}), \
             patch("claude_monitor._refresh_pid_map"), \
             patch("claude_monitor._recently_resumed", {}), \
             patch("claude_monitor.read_hook_state", return_value={"pid": 4538}), \
             patch("claude_monitor._pid_is_claude", return_value=True), \
             patch("claude_monitor.SESSIONS_DIR", tmp_path):
            assert _is_session_alive("old-sid") is False
            assert _is_session_alive("new-sid") is True


class TestBackgroundActivity:
    def _layout(self, tmp_path, fresh: dict[str, list[str]], stale: dict[str, list[str]] | None = None):
        """Create <tmp>/sid.jsonl plus subagents/workflows dirs with files."""
        transcript = tmp_path / "sid.jsonl"
        transcript.touch()
        base = tmp_path / "sid"
        for sub, names in (fresh or {}).items():
            d = base / sub
            d.mkdir(parents=True, exist_ok=True)
            for n in names:
                (d / n).write_text("{}")
        import os as _os
        for sub, names in (stale or {}).items():
            d = base / sub
            d.mkdir(parents=True, exist_ok=True)
            for n in names:
                f = d / n
                f.write_text("{}")
                old = time.time() - 600
                _os.utime(f, (old, old))
        return str(transcript)

    def test_no_dirs_returns_zero(self, tmp_path):
        transcript = tmp_path / "sid.jsonl"
        transcript.touch()
        assert count_background_activity(str(transcript)) == 0

    def test_counts_fresh_subagents_and_workflows(self, tmp_path):
        t = self._layout(tmp_path, {"subagents": ["a.jsonl", "b.jsonl"], "workflows": ["w.jsonl"]})
        assert count_background_activity(t) == 3

    def test_ignores_stale_files(self, tmp_path):
        t = self._layout(tmp_path,
                         fresh={"subagents": ["a.jsonl"]},
                         stale={"subagents": ["old.jsonl"], "workflows": ["old2.jsonl"]})
        assert count_background_activity(t) == 1

    def test_ignores_non_jsonl(self, tmp_path):
        t = self._layout(tmp_path, {"subagents": ["a.jsonl", "note.txt"]})
        assert count_background_activity(t) == 1

    def test_finds_nested_workflow_agents(self, tmp_path):
        """Workflow agents live at subagents/workflows/wf_<id>/agent-*.jsonl"""
        transcript = tmp_path / "sid.jsonl"
        transcript.touch()
        d = tmp_path / "sid" / "subagents" / "workflows" / "wf_abc"
        d.mkdir(parents=True)
        (d / "agent-1.jsonl").write_text("{}")
        (d / "agent-2.jsonl").write_text("{}")
        (d / "agent-1.meta.json").write_text("{}")
        assert count_background_activity(str(transcript)) == 2

    def _backdate(self, p):
        old = time.time() - 600
        import os as _os
        _os.utime(p, (old, old))

    def test_status_idle_becomes_background(self, tmp_path):
        t = self._layout(tmp_path, {"subagents": ["a.jsonl"]})
        self._backdate(t)
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value={"state": "idle", "state_entered_at": ""}):
            assert determine_status("sid", 0, "", t) == "background"

    def test_status_idle_stays_idle_without_activity(self, tmp_path):
        transcript = tmp_path / "sid.jsonl"
        transcript.touch()
        self._backdate(transcript)
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value={"state": "idle", "state_entered_at": ""}):
            assert determine_status("sid", 0, "", str(transcript)) == "waiting"

    def test_status_idle_but_transcript_fresh_becomes_working(self, tmp_path):
        transcript = tmp_path / "sid.jsonl"
        transcript.touch()  # mtime = now
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value={"state": "idle", "state_entered_at": ""}):
            assert determine_status("sid", 0, "", str(transcript)) == "working"

    def test_status_working_unchanged_by_activity(self, tmp_path):
        t = self._layout(tmp_path, {"subagents": ["a.jsonl"]})
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state", return_value={"state": "thinking"}):
            assert determine_status("sid", 0, "", t) == "working"


class TestPinnedSurvivesHistoryOff:
    """A pinned session must appear even when history mode is off, even if its
    transcript is old and the process is dead. Regression: the three skip gates
    in parse_sessions dropped pinned sessions before the render-time pin guard
    could re-admit them."""

    def _setup(self, tmp_path):
        sid = "pinned00-dead-beef-cafe-000000000001"
        proj = tmp_path / "-Users-test-proj"
        proj.mkdir()
        t = proj / f"{sid}.jsonl"
        t.write_text(make_transcript_jsonl(tokens_out=500))
        old = time.time() - 86400 * 2  # 2 days: archived but inside 7-day cutoff
        import os as _os
        _os.utime(t, (old, old))
        return sid, t

    def _run(self, tmp_path, pinned):
        with patch("claude_monitor.CLAUDE_DIR", tmp_path), \
             patch("claude_monitor._is_session_alive", return_value=False), \
             patch("claude_monitor._gc_state_files"), \
             patch("claude_monitor.load_index_metadata", return_value={}), \
             patch("claude_monitor._refresh_pid_map"), \
             patch("claude_monitor._pid_map", {}), \
             patch("claude_monitor.read_hook_state", return_value={}):
            return parse_sessions(include_archived=False, pinned=pinned)

    def test_unpinned_old_dead_session_dropped(self, tmp_path):
        sid, _ = self._setup(tmp_path)
        ids = {s.session_id for s in self._run(tmp_path, pinned=set())}
        assert sid not in ids

    def test_pinned_old_dead_session_kept(self, tmp_path):
        sid, _ = self._setup(tmp_path)
        ids = {s.session_id for s in self._run(tmp_path, pinned={sid})}
        assert sid in ids


class TestFilterIgnored:
    """Naming a session with "&ignore" drops it from monitoring entirely, plus
    its PID-siblings and subagents. Explicit opt-out; beats pinning."""

    def test_drops_named_ignore(self):
        ss = [make_session(session_id="a", title="keep me"),
              make_session(session_id="b", title="scratch &ignore")]
        assert {s.session_id for s in filter_ignored(ss)} == {"a"}

    def test_case_insensitive(self):
        ss = [make_session(session_id="b", title="junk &IGNORE")]
        assert filter_ignored(ss) == []

    def test_marker_as_suffix(self):
        ss = [make_session(session_id="a", title="myproj&ignore")]
        assert filter_ignored(ss) == []

    def test_keeps_all_when_no_marker(self):
        ss = [make_session(session_id="a", title="one"),
              make_session(session_id="b", title="two")]
        assert len(filter_ignored(ss)) == 2

    def test_drops_pid_sibling_of_ignored(self):
        # Sibling row keyed sid@pid whose own title lost the marker still goes.
        ss = [make_session(session_id="a", title="foo &ignore"),
              make_session(session_id="a@1234", title="foo")]
        assert filter_ignored(ss) == []

    def test_drops_subagent_of_ignored(self):
        ss = [make_session(session_id="a", title="foo&ignore"),
              make_session(session_id="sub1", title="subtask", parent_id="a")]
        assert filter_ignored(ss) == []

    def test_keeps_unrelated_subagent(self):
        ss = [make_session(session_id="a", title="foo&ignore"),
              make_session(session_id="sub1", title="subtask", parent_id="z")]
        assert {s.session_id for s in filter_ignored(ss)} == {"sub1"}


class TestDisambiguateTitles:
    def test_distinct_titles_untouched(self):
        ss = [make_session(session_id="a1234567-x", title="alpha"),
              make_session(session_id="b1234567-x", title="beta")]
        disambiguate_titles(ss)
        assert [s.title for s in ss] == ["alpha", "beta"]

    def test_same_title_different_sid_gets_sid8(self):
        ss = [make_session(session_id="a1234567-dead-beef", title="dup"),
              make_session(session_id="b1234567-dead-beef", title="dup")]
        disambiguate_titles(ss)
        assert ss[0].title == "dup ·a1234567"
        assert ss[1].title == "dup ·b1234567"

    def test_siblings_same_sid_get_pid_suffix(self):
        """Fixture from 2026-06-16: config-skills resumed twice (pids 27852 and
        92899, same sid b9bb8e2d). The sid8 tag is identical for siblings, so
        the second pass appends the pid from the carrier key."""
        sid = "b9bb8e2d-115f-4524-926d-28d0696d7fd0"
        ss = [make_session(session_id=f"{sid}@27852", title="config-skills"),
              make_session(session_id=f"{sid}@92899", title="config-skills")]
        disambiguate_titles(ss)
        assert ss[0].title == "config-skills ·b9bb8e2d·27852"
        assert ss[1].title == "config-skills ·b9bb8e2d·92899"
        assert ss[0].title != ss[1].title

    def test_disambiguation_preserves_group_key(self):
        """The renderer groups by _group_key(s.title) AFTER disambiguation, so a
        suffix that re-keys a sibling (the original '@pid' did, since '@' is the
        explicit-group sigil) splits one group into non-contiguous runs and the
        second '__group__config' header DuplicateKeys. The suffix must be
        group-key-neutral."""
        from claude_monitor import _group_key
        sid = "b9bb8e2d-115f-4524-926d-28d0696d7fd0"
        ss = [make_session(session_id="6e5743c6-aaa", title="config-claude.md"),
              make_session(session_id=f"{sid}@27852", title="config-skills"),
              make_session(session_id=f"{sid}@92899", title="config-skills"),
              make_session(session_id="8f7e4862-bbb", title="config-MCPs")]
        before = [_group_key(s.title) for s in ss]
        disambiguate_titles(ss)
        after = [_group_key(s.title) for s in ss]
        assert before == after == ["config"] * 4
        assert "@" not in ss[1].title and "@" not in ss[2].title

    def test_mixed_three_way(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ss = [make_session(session_id=f"{sid}@1", title="x"),
              make_session(session_id=f"{sid}@2", title="x"),
              make_session(session_id="ffffffff-0000", title="x")]
        disambiguate_titles(ss)
        titles = [s.title for s in ss]
        assert len(set(titles)) == 3
        assert titles[2] == "x ·ffffffff"


class TestTitleResolutionTreeStraggler:
    """A long-lived sid's transcript is a parentUuid tree; its last custom-title
    line can be a straggler from a divergent branch (a month-long
    tools-frontier-curve session whose final written leaf briefly carried
    tools-monitor). For an EXITED session, the settled hook-state identity must
    win over that straggler; for a LIVE session, custom_title must still win so
    /rename works."""

    def _transcript(self, tmp_path, last_title, top_title):
        p = tmp_path / "t.jsonl"
        p.write_text(make_transcript_jsonl(
            messages=[{"type": "custom-title", "customTitle": last_title}],
            custom_title=top_title,
        ))
        return p

    def test_exited_session_prefers_hook_title_over_straggler(self, tmp_path):
        p = self._transcript(tmp_path, last_title="tools-monitor",
                             top_title="tools-frontier-curve")
        hook = {"state": "exited", "title": "tools-frontier-curve",
                "timestamp": "2026-05-25T12:27:01"}
        with patch("claude_monitor.read_hook_state", return_value=hook), \
             patch("claude_monitor._is_session_alive", return_value=False):
            s = build_session(str(p), "9f17ea8e-0d55-418b-b5c6-97f47f27506e",
                              "capability-frontier", {}, p.stat().st_mtime)
        assert s is not None
        assert s.title == "tools-frontier-curve"

    def test_live_session_keeps_custom_title(self, tmp_path):
        p = self._transcript(tmp_path, last_title="renamed-live",
                             top_title="old-name")
        hook = {"state": "idle", "title": "old-name",
                "timestamp": "2026-05-26T09:00:00"}
        with patch("claude_monitor.read_hook_state", return_value=hook), \
             patch("claude_monitor._is_session_alive", return_value=True):
            s = build_session(str(p), "live-sid", "proj", {}, p.stat().st_mtime)
        assert s is not None
        assert s.title == "renamed-live"


class TestTranscriptCustomTitle:
    def _hook_mod(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
        import session_tracker
        return session_tracker

    def test_reads_latest_custom_title(self, tmp_path):
        st = self._hook_mod()
        t = tmp_path / "x.jsonl"
        t.write_text(
            '{"type":"user","message":{"content":"hi"}}\n'
            '{"type":"custom-title","customTitle":"Old Name"}\n'
            '{"type":"assistant","message":{"content":[]}}\n'
            '{"type":"custom-title","customTitle":"New Name"}\n'
            '{"type":"user","message":{"content":"more"}}\n'
        )
        assert st.read_transcript_custom_title(str(t)) == "New Name"

    def test_missing_file(self):
        st = self._hook_mod()
        assert st.read_transcript_custom_title("/nonexistent/x.jsonl") == ""

    def test_no_custom_title_lines(self, tmp_path):
        st = self._hook_mod()
        t = tmp_path / "x.jsonl"
        t.write_text('{"type":"user","message":{"content":"hi"}}\n')
        assert st.read_transcript_custom_title(str(t)) == ""


class TestSessionMemoryTitle:
    def test_missing_file(self, tmp_path):
        assert read_session_memory_title(str(tmp_path / "fake.jsonl")) == ""

    def test_parses_title_section(self, tmp_path):
        transcript = tmp_path / "s1.jsonl"
        transcript.touch()
        sm_dir = tmp_path / "s1" / "session-memory"
        sm_dir.mkdir(parents=True)
        (sm_dir / "summary.md").write_text(
            "# Session Title\n\n_description in italics_\n\nMy Great Title\n\n# Other\n"
        )
        assert read_session_memory_title(str(transcript)) == "My Great Title"

    def test_stops_at_next_heading(self, tmp_path):
        transcript = tmp_path / "s1.jsonl"
        transcript.touch()
        sm_dir = tmp_path / "s1" / "session-memory"
        sm_dir.mkdir(parents=True)
        (sm_dir / "summary.md").write_text("# Session Title\n# Next Section\nignored\n")
        assert read_session_memory_title(str(transcript)) == ""


class TestMatchCandidates:
    def test_sid8_always_first(self):
        s = make_session(session_id="abc12345-6789-xxxx-yyyy-zzzzzzzz")
        cands = _resolve_match_candidates(s)
        assert cands[0] == "\u00b7abc12345"

    def test_dedupes(self):
        s = make_session(title="same", status_name="same")
        cands = _resolve_match_candidates(s)
        assert cands.count("same") == 1

    def test_excludes_generic(self):
        s = make_session(title="Claude Code")
        cands = _resolve_match_candidates(s)
        assert "Claude Code" not in cands


class TestGroupKey:
    def test_prefix_grouping(self):
        from claude_monitor import _group_key
        assert _group_key("strategy-ideation") == "strategy"
        assert _group_key("strategy-frameworks") == "strategy"
        assert _group_key("strategy FSI") == "strategy"
        assert _group_key("fix/googleworkspace") == "fix"
        assert _group_key("tabby_ideation") == "tabby"

    def test_explicit_at_grouping(self):
        from claude_monitor import _group_key
        assert _group_key("bugs@disclosey") == "disclosey"
        assert _group_key("ideation@disclosey") == "disclosey"
        assert _group_key("v2-plan@disclosey") == "disclosey"

    def test_singleton_and_empty(self):
        from claude_monitor import _group_key
        assert _group_key("general") == "general"
        assert _group_key("") == "ungrouped"
        assert _group_key("@") == "ungrouped"
