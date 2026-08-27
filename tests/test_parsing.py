"""Tests for transcript parsing and session building."""

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import claude_monitor
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
    resume_session,
    find_next_actionable,
    _next_actionable_in_table_order,
    dedupe_scheduled_sessions,
    _mark_ready_seen,
    _resolve_match_candidates,
    _pid_is_claude,
    _refresh_process_comm_cache,
    _is_session_alive,
    count_background_activity,
    _drop_request_and_await_consumption,
    REQ_CONSUMED,
    REQ_SUPERSEDED,
    REQ_UNSERVED,
    _load_scan_cache_from_disk,
    _save_scan_cache_to_disk,
    _apply_standby_status,
    _apply_standby_to_all,
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
    def test_moderate_elapsed_is_done(self, _mock):
        """No more waiting/idle split by elapsed time — it's just done;
        how long ago is reported separately (format_ago on last_activity),
        not bucketed into a status value."""
        assert determine_status("test-id", time.time() - 60) == "done"

    @patch("claude_monitor._is_session_alive", return_value=True)
    def test_old_activity_is_done(self, _mock):
        assert determine_status("test-id", time.time() - 600) == "done"

    @patch("claude_monitor._is_session_alive", return_value=True)
    def test_no_activity(self, _mock):
        assert determine_status("test-id", 0) == "done"

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

    def test_stale_thinking_all_witnesses_quiet_decays_to_done(self, tmp_path):
        sessions_dir = self._pid_file(tmp_path, "idle")
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value=self._hook(4 * 3600)), \
             patch("claude_monitor._pid_map", {"sid": 63697}), \
             patch("claude_monitor.SESSIONS_DIR", sessions_dir):
            assert determine_status("sid", 0, "", self._old_transcript(tmp_path)) == "done"

    def test_stale_thinking_recent_entry_is_also_done(self, tmp_path):
        """No more waiting-vs-idle split by how recently 'thinking' froze —
        stale is stale, reported as done either way."""
        sessions_dir = self._pid_file(tmp_path, "idle")
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value=self._hook(600, entered_age_s=200)), \
             patch("claude_monitor._pid_map", {"sid": 63697}), \
             patch("claude_monitor.SESSIONS_DIR", sessions_dir):
            assert determine_status("sid", 0, "", self._old_transcript(tmp_path)) == "done"

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
    """_pid_is_claude() used to spawn its own `ps -p <pid>` subprocess per
    call; profiled 2026-08-16 against 136 real sessions, 236 such spawns
    cost ~1.5s of a ~6.5s cold parse_sessions() (the "Ctrl+Shift+N feels
    slow" complaint's second-biggest fixable chunk). Now backed by one
    `ps -Ao pid=,comm=` snapshot per 2s (_refresh_process_comm_cache),
    hence forcing _process_comm_ts to 0 in every test here: without it, a
    cache warmed by an earlier test in the same run makes the mock below
    never get called at all, and the test would pass by cache-hit accident
    rather than by actually exercising the parsing."""

    def _ps_result(self, pid: int, comm: str):
        m = type("R", (), {"stdout": f"{pid} {comm}\n", "returncode": 0})()
        return m

    def _force_cache_refresh(self):
        import claude_monitor as cm
        cm._process_comm_ts = 0

    def test_pid_is_claude_matches_cli(self):
        self._force_cache_refresh()
        with patch("claude_monitor.subprocess.run", return_value=self._ps_result(12345, "claude")):
            assert _pid_is_claude(12345) is True

    def test_pid_is_claude_rejects_recycled(self):
        self._force_cache_refresh()
        with patch("claude_monitor.subprocess.run",
                    return_value=self._ps_result(12345, "mdworker_shared")):
            assert _pid_is_claude(12345) is False

    def test_pid_is_claude_rejects_helpers(self):
        for comm in ("Claude Helper", "claude_crashpad_handler", "Claude.app", "claude-monitor"):
            self._force_cache_refresh()
            with patch("claude_monitor.subprocess.run", return_value=self._ps_result(12345, comm)):
                assert _pid_is_claude(12345) is False, comm

    def test_pid_is_claude_dead_pid(self):
        self._force_cache_refresh()
        with patch("claude_monitor.subprocess.run",
                    return_value=self._ps_result(1, "launchd")):
            assert _pid_is_claude(99999) is False

    def test_pid_is_claude_falls_back_to_stale_cache_on_transient_failure(self):
        """A momentary `ps` failure (timeout, resource limits) must not
        wipe out an otherwise-good cache and make every session look dead."""
        import claude_monitor as cm
        self._force_cache_refresh()
        with patch("claude_monitor.subprocess.run",
                    return_value=self._ps_result(12345, "claude")):
            assert _pid_is_claude(12345) is True
        cm._process_comm_ts = 0
        with patch("claude_monitor.subprocess.run", side_effect=OSError("boom")):
            assert _pid_is_claude(12345) is True  # stale cache, not wiped

    def test_refresh_process_comm_cache_parses_multiple_pids(self):
        import claude_monitor as cm
        self._force_cache_refresh()
        out = "  1 /sbin/launchd\n12345 /opt/homebrew/bin/claude\n99999 mdworker_shared\n"
        result = type("R", (), {"stdout": out, "returncode": 0})()
        with patch("claude_monitor.subprocess.run", return_value=result):
            _refresh_process_comm_cache()
        assert cm._process_comm_cache[1] == "/sbin/launchd"
        assert cm._process_comm_cache[12345] == "/opt/homebrew/bin/claude"
        assert cm._process_comm_cache[99999] == "mdworker_shared"

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

    def test_stale_hook_pid_falls_through_to_resume_grace_not_dead(self, tmp_path):
        """Fixture from 2026-08-15 (config-MCPs, sid 8f7e4862): the hook state
        still names an old PID that has since been recycled to a DIFFERENT
        live session. The old code asserted dead outright on that mismatch,
        which raced past the 60s post-resume grace period — a rapid second
        click read the just-launched session as not-alive and fired a
        duplicate resume, which Claude Code's own single-instance guard then
        killed (the reported 'coughing to a blank Ghostty'). The stale hook
        lead must be a dead end, not a verdict: grace still wins."""
        (tmp_path / "4538.json").write_text('{"sessionId": "unrelated-sid", "pid": 4538}')
        with patch("claude_monitor._pid_map", {}), \
             patch("claude_monitor._refresh_pid_map"), \
             patch("claude_monitor._recently_resumed", {"just-resumed-sid": time.time()}), \
             patch("claude_monitor.read_hook_state", return_value={"pid": 4538}), \
             patch("claude_monitor._pid_is_claude", return_value=True), \
             patch("claude_monitor.SESSIONS_DIR", tmp_path):
            assert _is_session_alive("just-resumed-sid") is True

    def test_resume_session_busts_pid_map_cache(self, tmp_path):
        """A successful resume must invalidate the PID-map cache itself, not
        rely on each caller to remember: one caller (the jump action's
        resume-fallback) didn't, so a fast second click could still read a
        stale pre-resume map and see 'not alive', firing a duplicate resume."""
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("{}\n")
        s = make_session(session_id="sid-1", transcript_path=str(transcript))
        ok_result = type("R", (), {"stdout": "Ghostty", "returncode": 0})()
        import claude_monitor
        claude_monitor._pid_map_ts = 12345.0
        with patch("claude_monitor.subprocess.run", return_value=ok_result), \
             patch("claude_monitor._derive_cwd_from_transcript", return_value=None):
            assert resume_session(s) is True
        assert claude_monitor._pid_map_ts == 0

    def test_resume_session_never_sends_synthetic_keystroke(self, tmp_path):
        """Fixture from 2026-08-15: any System Events keystroke/keyCode sent
        to open a new window/tab, real key or fake modifier, any combo,
        fires Claude Nest's push-to-talk hotkey as a side effect (confirmed
        live from a bare terminal osascript with no claude-monitor involved
        at all — it reacts to synthetic input generically, not to a specific
        key). resume_session() must open the window through Ghostty's own
        newWindow scripting, never se.keystroke/se.keyCode."""
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("{}\n")
        s = make_session(session_id="sid-1", transcript_path=str(transcript))
        with patch("claude_monitor.subprocess.run") as run, \
             patch("claude_monitor._derive_cwd_from_transcript", return_value=None):
            run.return_value = type("R", (), {"stdout": "Ghostty", "returncode": 0})()
            resume_session(s)
            jxa = run.call_args[0][0][-1]
        assert "se.keystroke" not in jxa
        assert "se.keyCode" not in jxa
        assert "newWindow" in jxa


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

    def test_status_idle_with_background_activity_is_working(self, tmp_path):
        """Background activity (a live subagent/workflow dir) folds into
        'working' rather than a separate status — it's still busy, just not
        the foreground turn. generate_activity() reports the count via
        s.background_count, not the status value."""
        t = self._layout(tmp_path, {"subagents": ["a.jsonl"]})
        self._backdate(t)
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value={"state": "idle", "state_entered_at": ""}):
            assert determine_status("sid", 0, "", t) == "working"

    def test_status_idle_without_activity_is_done(self, tmp_path):
        transcript = tmp_path / "sid.jsonl"
        transcript.touch()
        self._backdate(transcript)
        with patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor.read_hook_state",
                   return_value={"state": "idle", "state_entered_at": ""}):
            assert determine_status("sid", 0, "", str(transcript)) == "done"

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

    def _setup(self, tmp_path, age_days=2):
        sid = "pinned00-dead-beef-cafe-000000000001"
        proj = tmp_path / "-Users-test-proj"
        proj.mkdir()
        t = proj / f"{sid}.jsonl"
        t.write_text(make_transcript_jsonl(tokens_out=500))
        old = time.time() - 86400 * age_days
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

    def test_pinned_ancient_session_never_expires(self, tmp_path):
        """Fixture from 2026-08-16 (Max: 'pins should stay until I unpin
        them'). A prior version made a pin's exemption expire after 7 days
        (archive_cutoff) — Max corrected that: a pin is permanent, full stop.
        Whether an inactive pin is currently SHOWN is a separate concern
        (Ctrl+O / hide_inactive_pins in the TUI), not an age question here."""
        sid, _ = self._setup(tmp_path, age_days=400)
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


class TestFindNextActionable:
    """Fixture from 2026-08-16: "n" (in-app) and Ctrl+Shift+N (from anywhere
    on the machine) share this exact selection so they are one feature, not
    two independent reimplementations of "what needs you"."""

    def test_no_actionable_sessions_returns_none(self):
        sessions = [make_session(session_id="a", status="working"),
                    make_session(session_id="b", status="closed")]
        assert find_next_actionable(sessions, None) is None

    def test_first_call_returns_first_by_priority_then_age(self):
        older_done = make_session(session_id="done-old", status="done", last_activity=100)
        newer_done = make_session(session_id="done-new", status="done", last_activity=200)
        approval = make_session(session_id="approve", status="needs_approval", last_activity=150)
        sessions = [older_done, newer_done, approval]
        # needs_approval outranks done regardless of age
        assert find_next_actionable(sessions, None).session_id == "approve"

    def test_done_sorted_oldest_first_when_no_approval(self):
        older = make_session(session_id="older", status="done", last_activity=100)
        newer = make_session(session_id="newer", status="done", last_activity=200)
        assert find_next_actionable([newer, older], None).session_id == "older"

    def test_cycles_from_after_sid(self):
        a = make_session(session_id="a", status="done", last_activity=100)
        b = make_session(session_id="b", status="done", last_activity=200)
        c = make_session(session_id="c", status="done", last_activity=300)
        sessions = [a, b, c]
        assert find_next_actionable(sessions, "a").session_id == "b"
        assert find_next_actionable(sessions, "b").session_id == "c"

    def test_wraps_around_after_last(self):
        a = make_session(session_id="a", status="done", last_activity=100)
        b = make_session(session_id="b", status="done", last_activity=200)
        assert find_next_actionable([a, b], "b").session_id == "a"

    def test_unknown_after_sid_starts_from_first(self):
        a = make_session(session_id="a", status="done", last_activity=100)
        b = make_session(session_id="b", status="done", last_activity=200)
        # e.g. the previously-cursored session closed and dropped off the
        # actionable list entirely between one call and the next
        assert find_next_actionable([a, b], "stale-sid-no-longer-actionable").session_id == "a"

    def test_ignores_subagents(self):
        parent = make_session(session_id="p", status="working")
        sub = make_session(session_id="s", status="done", is_subagent=True)
        assert find_next_actionable([parent, sub], None) is None

    def test_excludes_already_seen_done_sessions(self):
        """Bug reported by Max (2026-08-17): "I don't want ctrl-shift-n to
        jump me to an already read, redundantly... it keeps jumping me to
        X and I don't need it, I already jumped and took no action" /
        "since the unread isn't working the READY state is sticking and I
        am wasting time checking things I already decided to deal with
        later." A session already in acked_ready must not be a candidate
        at all, not just sorted last."""
        seen = make_session(session_id="seen", status="done", last_activity=100)
        unseen = make_session(session_id="unseen", status="done", last_activity=50)
        result = find_next_actionable([seen, unseen], None, acked_ready={"seen"})
        assert result.session_id == "unseen"

    def test_returns_none_when_all_actionable_are_seen(self):
        """No re-visiting a seen session just because it's the only one
        left; nothing new means nothing to jump to."""
        seen = make_session(session_id="seen", status="done")
        assert find_next_actionable([seen], None, acked_ready={"seen"}) is None

    def test_needs_approval_never_excluded_by_acked_ready(self):
        """acked_ready only ever holds done sessions in real usage, but
        even if a stale/bogus entry matched a needs_approval sid, a
        blocking approval request must still surface: it's the one status
        that's actually blocking, seen or not."""
        approval = make_session(session_id="a", status="needs_approval")
        result = find_next_actionable([approval], None, acked_ready={"a"})
        assert result.session_id == "a"

    def test_acked_ready_none_matches_default_behavior(self):
        s = make_session(session_id="s", status="done")
        assert find_next_actionable([s], None, acked_ready=None) is not None
        assert find_next_actionable([s], None) is not None


class TestNextActionableInTableOrder:
    """The "n" key's own selection, distinct from find_next_actionable()'s
    urgency-priority cycling (used only by Ctrl+Shift+N, which has no
    visible cursor to reason about). Walks flat_rows in its own order
    (whatever the table is currently sorted/grouped by) so "n" never
    passes over a row you can see is actionable to reach a more "urgent"
    one further down (Max, 2026-08-17: "naked n just skipped a READY
    claude row")."""

    def test_no_current_index_starts_from_first_actionable(self):
        a = make_session(session_id="a", status="working")
        b = make_session(session_id="b", status="done")
        assert _next_actionable_in_table_order([a, b], None).session_id == "b"

    def test_walks_forward_from_current_index(self):
        a = make_session(session_id="a", status="done")
        b = make_session(session_id="b", status="done")
        c = make_session(session_id="c", status="done")
        assert _next_actionable_in_table_order([a, b, c], 0).session_id == "b"

    def test_skips_non_actionable_rows_in_between(self):
        a = make_session(session_id="a", status="done")
        working = make_session(session_id="w", status="working")
        c = make_session(session_id="c", status="done")
        assert _next_actionable_in_table_order([a, working, c], 0).session_id == "c"

    def test_never_jumps_past_an_earlier_actionable_row_for_a_later_one(self):
        """The exact scenario the bug report matched: an old-code priority
        cycle would land on a further, "more urgent" row and skip a
        visually-adjacent READY one; table order must not do that."""
        a = make_session(session_id="a", status="done", last_activity=400)
        b = make_session(session_id="b", status="done", last_activity=100)  # "oldest"
        c = make_session(session_id="c", status="done", last_activity=300)
        d = make_session(session_id="d", status="done", last_activity=200)
        # From b (index 1), table order says c is next; a priority-cycle
        # sorted by last_activity (b, d, c, a) would have said d instead.
        assert _next_actionable_in_table_order([a, b, c, d], 1).session_id == "c"

    def test_wraps_around(self):
        a = make_session(session_id="a", status="done")
        b = make_session(session_id="b", status="done")
        assert _next_actionable_in_table_order([a, b], 1).session_id == "a"

    def test_empty_returns_none(self):
        assert _next_actionable_in_table_order([], None) is None

    def test_no_actionable_rows_returns_none(self):
        a = make_session(session_id="a", status="working")
        assert _next_actionable_in_table_order([a], 0) is None

    def test_ignores_subagents(self):
        parent = make_session(session_id="p", status="working")
        sub = make_session(session_id="s", status="done", is_subagent=True)
        assert _next_actionable_in_table_order([parent, sub], None) is None


class TestMarkReadySeen:
    """The write half of the READY read/unread feature: jumping to a done
    session persists that it's been seen; jumping to anything else is a
    no-op, since the seen mark only ever means anything for status done.
    Since 2026-08-18 each ack is [visit_count, last_activity_at_mark]: the
    count drives the second de-emphasis tier ("checked twice"), the stamp
    lets the ack expire once the session does new work (see
    TestSeenAckExpiresOnNewActivity)."""

    def test_marks_done_session_seen(self):
        with patch("claude_monitor.load_prefs", return_value={}), \
             patch("claude_monitor.save_prefs") as save:
            _mark_ready_seen("sid-1", "done", 100.0)
            save.assert_called_once()
            assert save.call_args[0][0]["acked_ready"] == {"sid-1": [1, 100.0]}

    def test_ignores_non_done_status(self):
        with patch("claude_monitor.load_prefs", return_value={}), \
             patch("claude_monitor.save_prefs") as save:
            _mark_ready_seen("sid-1", "working", 100.0)
            save.assert_not_called()

    def test_second_visit_increments_count(self):
        """Max, 2026-08-18: "when something is checked twice without action
        please unbold it in ready state." The count is what the renderer
        keys the second de-emphasis tier off, so it must actually climb
        when the session has NOT been active since the first mark."""
        with patch("claude_monitor.load_prefs",
                   return_value={"acked_ready": {"sid-1": [1, 100.0]}}), \
             patch("claude_monitor.save_prefs") as save:
            _mark_ready_seen("sid-1", "done", 100.0)
            assert save.call_args[0][0]["acked_ready"] == {"sid-1": [2, 100.0]}

    def test_count_restarts_when_session_was_active_since_last_mark(self):
        """"Checked twice" must never span two separate done episodes: if
        the session did work between visits, this visit is the FIRST look
        at the new result, so the count resets to 1 and the stamp moves."""
        with patch("claude_monitor.load_prefs",
                   return_value={"acked_ready": {"sid-1": [1, 100.0]}}), \
             patch("claude_monitor.save_prefs") as save:
            _mark_ready_seen("sid-1", "done", 250.0)
            assert save.call_args[0][0]["acked_ready"] == {"sid-1": [1, 250.0]}

    def test_legacy_list_shape_is_upgraded_in_place(self):
        """A prefs file written by the original build holds a plain list;
        each entry reads as one visit with stamp 0.0 (never voids, matching
        its old never-expiring semantics) and is rewritten stamped."""
        with patch("claude_monitor.load_prefs", return_value={"acked_ready": ["sid-1"]}), \
             patch("claude_monitor.save_prefs") as save:
            _mark_ready_seen("sid-2", "done", 50.0)
            assert save.call_args[0][0]["acked_ready"] == {"sid-1": [1, 0.0], "sid-2": [1, 50.0]}

    def test_legacy_count_only_shape_is_upgraded_in_place(self):
        """The interim {sid: count} shape (one build, 2026-08-18 am)."""
        with patch("claude_monitor.load_prefs", return_value={"acked_ready": {"sid-1": 3}}), \
             patch("claude_monitor.save_prefs") as save:
            _mark_ready_seen("sid-2", "done", 50.0)
            assert save.call_args[0][0]["acked_ready"] == {"sid-1": [3, 0.0], "sid-2": [1, 50.0]}

    def test_preserves_other_seen_sessions(self):
        with patch("claude_monitor.load_prefs",
                   return_value={"acked_ready": {"sid-1": [3, 10.0]}}), \
             patch("claude_monitor.save_prefs") as save:
            _mark_ready_seen("sid-2", "done", 50.0)
            assert save.call_args[0][0]["acked_ready"] == {"sid-1": [3, 10.0], "sid-2": [1, 50.0]}

    def test_cap_evicts_least_recently_marked_not_first_ever_marked(self):
        """Advisor review 2026-08-18: incrementing a dict key does not move
        it, so a naive [-CAP:] slice evicted the longest-ago-FIRST-marked
        session, which could be a still-done one you revisited yesterday,
        while keeping dead sids. Re-marking must move the key to the end."""
        import claude_monitor as cm
        old = {f"old-{i}": [1, 1.0] for i in range(cm._ACKED_READY_CAP)}
        # old-0 was marked first; re-marking it now must protect it.
        with patch("claude_monitor.load_prefs", return_value={"acked_ready": dict(old)}), \
             patch("claude_monitor.save_prefs") as save:
            _mark_ready_seen("old-0", "done", 1.0)  # bump old-0 to most recent
            saved = save.call_args[0][0]["acked_ready"]
            assert list(saved)[-1] == "old-0"
        with patch("claude_monitor.load_prefs", return_value={"acked_ready": saved}), \
             patch("claude_monitor.save_prefs") as save:
            _mark_ready_seen("brand-new", "done", 2.0)  # overflow by one
            saved = save.call_args[0][0]["acked_ready"]
            assert len(saved) == cm._ACKED_READY_CAP
            assert "old-0" in saved          # recently re-marked: kept
            assert "old-1" not in saved      # least recently marked: evicted
            assert "brand-new" in saved


class TestSeenAckExpiresOnNewActivity:
    """BUG found by advisor review 2026-08-18 (introduced by #51): removing
    the refresh cycle's prune-and-write-back fixed a race but also removed
    the only thing that ever expired a seen-mark, so an ack survived
    done -> working -> done forever. Jump to READY session A, give it new
    work, A finishes 20 minutes later: A rendered as already-seen and
    Ctrl+Shift+N skipped it, a freshly finished session you had never
    looked at, invisible until Shift+R. Now every reader compares the
    ack's activity stamp against the session's current last_activity."""

    def test_ack_is_live_while_session_has_not_moved(self):
        from claude_monitor import _effective_seen_count
        s = make_session(session_id="a", status="done", last_activity=100.0)
        assert _effective_seen_count({"a": [2, 100.0]}, s) == 2

    def test_ack_is_void_once_session_has_new_activity(self):
        from claude_monitor import _effective_seen_count
        s = make_session(session_id="a", status="done", last_activity=250.0)
        assert _effective_seen_count({"a": [2, 100.0]}, s) == 0

    def test_non_done_is_never_seen(self):
        from claude_monitor import _effective_seen_count
        s = make_session(session_id="a", status="working", last_activity=100.0)
        assert _effective_seen_count({"a": [1, 100.0]}, s) == 0

    def test_legacy_set_shape_counts_once_and_never_expires(self):
        from claude_monitor import _effective_seen_count
        s = make_session(session_id="a", status="done", last_activity=999.0)
        assert _effective_seen_count({"a"}, s) == 1

    def test_ctrl_shift_n_revisits_a_session_that_finished_new_work(self):
        """The user-visible half: after A does new work and becomes done
        again, it must be a jump candidate again."""
        a = make_session(session_id="a", status="done", last_activity=250.0)
        assert find_next_actionable([a], None, acked_ready={"a": [1, 100.0]}) is not None

    def test_ctrl_shift_n_still_skips_a_genuinely_seen_session(self):
        a = make_session(session_id="a", status="done", last_activity=100.0)
        assert find_next_actionable([a], None, acked_ready={"a": [1, 100.0]}) is None


class TestDedupeScheduledSessions:
    """Shared by the TUI's own refresh and jump_to_next_actionable() so the
    headless --jump-next CLI sees the exact same candidate set the in-app
    "n" key does — they diverged once before this was pulled out into one
    function (2026-08-16), letting the two disagree on a shared cursor."""

    def test_keeps_only_latest_per_cwd_title(self):
        old = make_session(session_id="old", is_scheduled=True, cwd="/p", title="scout",
                           last_activity=100)
        new = make_session(session_id="new", is_scheduled=True, cwd="/p", title="scout",
                           last_activity=200)
        result = dedupe_scheduled_sessions([old, new], set())
        ids = {s.session_id for s in result}
        assert ids == {"new"}

    def test_pinned_scheduled_session_always_kept(self):
        old = make_session(session_id="old", is_scheduled=True, cwd="/p", title="scout",
                           last_activity=100)
        new = make_session(session_id="new", is_scheduled=True, cwd="/p", title="scout",
                           last_activity=200)
        result = dedupe_scheduled_sessions([old, new], {"old"})
        ids = {s.session_id for s in result}
        assert ids == {"old", "new"}

    def test_non_scheduled_sessions_untouched(self):
        s = make_session(session_id="s", is_scheduled=False)
        assert dedupe_scheduled_sessions([s], set()) == [s]


class TestDropRequestAndAwaitConsumption:
    """The write half of Ctrl+Shift+N / --restart's fast path: drop a
    unique token into the shared jump-request file and wait for a live
    monitor's poller to consume it. Two bugs a review caught here
    (2026-08-16): reporting success when a second overlapping request's
    write was consumed instead of ours, and burning a full timeout before
    falling back when no monitor is running at all."""

    def _use_scratch_path(self, tmp_path, monkeypatch):
        import claude_monitor as cm
        scratch = tmp_path / "jump-request"
        monkeypatch.setattr(cm, "JUMP_REQUEST_PATH", scratch)
        return scratch

    def test_unserved_immediately_when_no_monitor_running(self, tmp_path, monkeypatch):
        scratch = self._use_scratch_path(tmp_path, monkeypatch)
        with patch("claude_monitor._a_monitor_is_running", return_value=False):
            start = time.time()
            result = _drop_request_and_await_consumption("__test__", timeout=1.0)
            elapsed = time.time() - start
        assert result == REQ_UNSERVED
        assert elapsed < 0.2  # must not burn the full timeout
        assert not scratch.exists()  # never even wrote the request

    def test_consumed_when_taken_unmodified(self, tmp_path, monkeypatch):
        scratch = self._use_scratch_path(tmp_path, monkeypatch)

        def consume():
            time.sleep(0.05)
            scratch.unlink(missing_ok=True)

        with patch("claude_monitor._a_monitor_is_running", return_value=True):
            threading.Thread(target=consume).start()
            result = _drop_request_and_await_consumption("__test__", timeout=1.0, poll=0.01)
        assert result == REQ_CONSUMED

    def test_superseded_when_overwritten_by_another_request(self, tmp_path, monkeypatch):
        """Two overlapping --jump-next presses (or one racing an unrelated
        click-to-jump link) must not both report success when only one of
        their writes was ever actually read by the poller."""
        scratch = self._use_scratch_path(tmp_path, monkeypatch)

        def clobber():
            time.sleep(0.05)
            scratch.write_text("__test__:someone-else:999")

        with patch("claude_monitor._a_monitor_is_running", return_value=True):
            threading.Thread(target=clobber).start()
            result = _drop_request_and_await_consumption("__test__", timeout=1.0, poll=0.01)
        assert result == REQ_SUPERSEDED

    def test_times_out_and_cleans_up_when_nothing_consumes_it(self, tmp_path, monkeypatch):
        scratch = self._use_scratch_path(tmp_path, monkeypatch)
        with patch("claude_monitor._a_monitor_is_running", return_value=True):
            result = _drop_request_and_await_consumption("__test__", timeout=0.1, poll=0.02)
        assert result == REQ_UNSERVED
        assert not scratch.exists()

    def test_consumed_at_the_last_instant_is_not_reported_unserved(self, tmp_path, monkeypatch):
        """RISK found by advisor review 2026-08-18: the monitor's main
        thread can stall past the client's timeout (a menu jump runs
        focus_terminal_session synchronously on it; Shift+R's git pull can
        block ~15s), so the poller may take the token in the final instant.
        The client used to blindly unlink and report unserved, sending
        --jump-next down the cold path: two jumps, next_cursor advanced
        twice. It must re-read at the deadline and report CONSUMED."""
        scratch = self._use_scratch_path(tmp_path, monkeypatch)
        real_monotonic = time.monotonic
        # Consume the token exactly when the loop's deadline check trips:
        # sleep is patched so the loop body runs once, then the "deadline"
        # re-read must observe the file already gone.
        state = {"polls": 0}

        def fake_sleep(_):
            state["polls"] += 1
            scratch.unlink(missing_ok=True)  # poller takes it during our sleep

        with patch("claude_monitor._a_monitor_is_running", return_value=True), \
             patch("claude_monitor.time.sleep", side_effect=fake_sleep):
            result = _drop_request_and_await_consumption("__test__", timeout=0.0001, poll=0.01)
        assert result == REQ_CONSUMED

    def test_superseded_at_the_deadline_is_not_reported_unserved(self, tmp_path, monkeypatch):
        """Same deadline path, other branch: if a second request overwrote
        ours by the deadline, the poller will serve THAT one, so a jump is
        still coming; reporting unserved would double-jump."""
        scratch = self._use_scratch_path(tmp_path, monkeypatch)

        def fake_sleep(_):
            scratch.write_text("__test__:someone-else:999")

        with patch("claude_monitor._a_monitor_is_running", return_value=True), \
             patch("claude_monitor.time.sleep", side_effect=fake_sleep):
            result = _drop_request_and_await_consumption("__test__", timeout=0.0001, poll=0.01)
        assert result == REQ_SUPERSEDED
        assert scratch.exists()  # never unlink a token that is not ours


class TestScanCacheDiskPersistence:
    """Fixture from 2026-08-17 (Max: "performance improve"): a cold monitor
    launch or Shift+R restart used to re-parse every transcript's JSONL
    from scratch every single time, ~4.5s against real session counts
    measured that day, because the in-memory scan cache started empty in
    every new process. Persisting it to disk (keyed by path+mtime, same
    as the in-memory cache) cut a fresh process's parse_sessions() to
    ~0.3s once a prior run had already scanned the same, unchanged files."""

    def _reset_load_guard(self, monkeypatch):
        import claude_monitor as cm
        monkeypatch.setattr(cm, "_scan_cache_loaded_from_disk", False)

    def test_save_then_load_round_trips(self, tmp_path, monkeypatch):
        import claude_monitor as cm
        cache_path = tmp_path / "scan-cache.json"
        transcript = tmp_path / "real.jsonl"
        transcript.write_text("{}")  # must exist: _save_scan_cache_to_disk prunes dead paths
        monkeypatch.setattr(cm, "SCAN_CACHE_PATH", cache_path)
        monkeypatch.setattr(cm, "_scan_cache", {str(transcript): (123.0, {"tokens_in": 500})})
        _save_scan_cache_to_disk()
        assert cache_path.exists()

        monkeypatch.setattr(cm, "_scan_cache", {})
        self._reset_load_guard(monkeypatch)
        _load_scan_cache_from_disk()
        assert cm._scan_cache[str(transcript)] == (123.0, {"tokens_in": 500})

    def test_prunes_entries_for_deleted_transcripts(self, tmp_path, monkeypatch):
        """Bug caught by review (2026-08-17): without pruning,
        monitor-scan-cache.json only ever grows across every restart for
        as long as the tool is used, retaining scan results (including
        last_assistant_text snippets) for transcripts long since deleted."""
        import claude_monitor as cm
        cache_path = tmp_path / "scan-cache.json"
        alive = tmp_path / "alive.jsonl"
        alive.write_text("{}")
        monkeypatch.setattr(cm, "SCAN_CACHE_PATH", cache_path)
        monkeypatch.setattr(cm, "_scan_cache", {
            str(alive): (1.0, {"x": 1}),
            "/deleted/no-longer-exists.jsonl": (2.0, {"y": 2}),
        })
        _save_scan_cache_to_disk()
        saved = json.loads(cache_path.read_text())
        assert str(alive) in saved
        assert "/deleted/no-longer-exists.jsonl" not in saved

    def test_missing_file_is_a_silent_noop(self, tmp_path, monkeypatch):
        import claude_monitor as cm
        monkeypatch.setattr(cm, "SCAN_CACHE_PATH", tmp_path / "does-not-exist.json")
        monkeypatch.setattr(cm, "_scan_cache", {"keep": (1.0, {})})
        self._reset_load_guard(monkeypatch)
        _load_scan_cache_from_disk()  # must not raise or wipe the existing cache
        assert cm._scan_cache == {"keep": (1.0, {})}

    def test_corrupt_file_is_a_silent_noop(self, tmp_path, monkeypatch):
        import claude_monitor as cm
        cache_path = tmp_path / "scan-cache.json"
        cache_path.write_text("not valid json {{{")
        monkeypatch.setattr(cm, "SCAN_CACHE_PATH", cache_path)
        monkeypatch.setattr(cm, "_scan_cache", {"keep": (1.0, {})})
        self._reset_load_guard(monkeypatch)
        _load_scan_cache_from_disk()
        assert cm._scan_cache == {"keep": (1.0, {})}

    def test_loads_only_once_per_process(self, tmp_path, monkeypatch):
        """A second call must not re-read the file (and, more importantly,
        must not clobber cache entries written since the first load by an
        in-process scan_full_file() call)."""
        import claude_monitor as cm
        cache_path = tmp_path / "scan-cache.json"
        cache_path.write_text('{"/a": [1.0, {"x": 1}]}')
        monkeypatch.setattr(cm, "SCAN_CACHE_PATH", cache_path)
        monkeypatch.setattr(cm, "_scan_cache", {})
        self._reset_load_guard(monkeypatch)

        _load_scan_cache_from_disk()
        assert "/a" in cm._scan_cache

        # A fresh in-process scan adds a new entry...
        cm._scan_cache["/b"] = (2.0, {"y": 2})
        # ...and the file changes underneath (simulating another process)...
        cache_path.write_text('{"/a": [1.0, {"x": 1}], "/c": [3.0, {"z": 3}]}')
        # ...but the second call is a no-op: it must not reload and wipe "/b".
        _load_scan_cache_from_disk()
        assert cm._scan_cache == {"/a": (1.0, {"x": 1}), "/b": (2.0, {"y": 2})}

    def test_scan_full_file_loads_disk_cache_on_first_call(self, tmp_path, monkeypatch):
        """The actual integration point: a cold scan_full_file() call for a
        transcript another process already scanned (same path, same
        mtime) must return the disk-cached result instead of re-reading
        and re-parsing the file."""
        import claude_monitor as cm
        transcript = tmp_path / "t.jsonl"
        transcript.write_text('{"type": "user"}\n')
        real_mtime = transcript.stat().st_mtime

        cache_path = tmp_path / "scan-cache.json"
        cached_result = {"tokens_in": 99999, "model_id": "from-disk-cache"}
        cache_path.write_text(json.dumps({str(transcript): [real_mtime, cached_result]}))
        monkeypatch.setattr(cm, "SCAN_CACHE_PATH", cache_path)
        monkeypatch.setattr(cm, "_scan_cache", {})
        self._reset_load_guard(monkeypatch)

        result = scan_full_file(str(transcript))
        assert result["model_id"] == "from-disk-cache"
        assert result["tokens_in"] == 99999


class TestApplyStandbyStatus:
    """Max, 2026-08-18: "anything with config- should go to STANDBY not
    READY as a canonical customization for me." Only an idle (done)
    config-* session is relabelled; a config desk that is actively working
    or blocked on approval is exactly as urgent as any other session."""

    def test_done_config_session_becomes_standby(self):
        assert _apply_standby_status("done", "config-skills") == "standby"

    def test_covers_every_desk_prefix_variant(self):
        for title in ("config-LEAD", "config-MCPs", "config-claude.md", "config-hooks"):
            assert _apply_standby_status("done", title) == "standby", title

    def test_working_config_session_stays_working(self):
        assert _apply_standby_status("working", "config-skills") == "working"

    def test_needs_approval_config_session_stays_urgent(self):
        assert _apply_standby_status("needs_approval", "config-skills") == "needs_approval"

    def test_closed_and_archived_untouched(self):
        assert _apply_standby_status("closed", "config-skills") == "closed"
        assert _apply_standby_status("archived", "config-skills") == "archived"

    def test_non_config_done_session_stays_done(self):
        assert _apply_standby_status("done", "frontier-curve") == "done"

    def test_prefix_is_exact_not_substring(self):
        """'reconfig-x' or 'my-config-x' are not desk sessions."""
        assert _apply_standby_status("done", "reconfig-tool") == "done"
        assert _apply_standby_status("done", "my-config-notes") == "done"


class TestStandbyAppliedOnEveryConstructionPath:
    """BUG found by advisor review 2026-08-18: standby was first wired into
    build_session() only, missing the PID-file orphan pass (hardcodes
    "done") and the multi-PID sibling split (maps idle -> "done" over the
    base row). A double-resumed config-MCPs, the exact case the sibling
    pass exists for, still rendered bold READY and re-entered Ctrl+Shift+N's
    candidates. Standby is now one final pass over the whole list, so no
    construction path can miss it."""

    def test_sibling_split_rows_get_standby(self):
        sid = "b9bb8e2d-115f-4524-926d-28d0696d7fd0"
        # Exactly what the sibling split produces: bare "done" on config rows.
        rows = [make_session(session_id=f"{sid}@27852", title="config-MCPs", status="done"),
                make_session(session_id=f"{sid}@92899", title="config-MCPs", status="done")]
        _apply_standby_to_all(rows)
        assert [r.status for r in rows] == ["standby", "standby"]

    def test_orphan_pass_row_gets_standby(self):
        # The orphan pass builds a session with no transcript and status "done".
        row = make_session(title="config-hooks", status="done", transcript_path="")
        _apply_standby_to_all([row])
        assert row.status == "standby"

    def test_busy_sibling_stays_working(self):
        row = make_session(title="config-skills", status="working")
        _apply_standby_to_all([row])
        assert row.status == "working"

    def test_non_config_rows_untouched(self):
        rows = [make_session(session_id="a", title="frontier-curve", status="done"),
                make_session(session_id="b", title="EBC-Visa", status="working")]
        _apply_standby_to_all(rows)
        assert [r.status for r in rows] == ["done", "working"]

    def test_standby_desk_is_not_a_ctrl_shift_n_candidate(self):
        """The user-visible consequence the bug produced."""
        desk = make_session(session_id="d", title="config-MCPs", status="done")
        _apply_standby_to_all([desk])
        assert find_next_actionable([desk], None) is None


class TestResumeCommandForSiblingRows:
    def test_resume_uses_bare_conversation_id_for_a_sibling_row(self):
        """A double-resumed conversation is listed as 'uuid@pid' rows;
        `claude --resume` must get the bare uuid (found by the layout
        restore test, 2026-08-23, but it bites the menu's Resume too)."""
        from claude_monitor import _resume_command_for
        sib = make_session(session_id="cafecafe-0000@12345", project_path=str(Path.home()))
        cmd, cwd = _resume_command_for(sib)
        assert cmd == "claude --resume cafecafe-0000"
        assert cwd == str(Path.home())

    def test_resume_falls_back_to_home_when_every_cwd_candidate_is_gone(self):
        from claude_monitor import _resume_command_for
        s = make_session(session_id="a", project_path="/definitely/not/here",
                         cwd="/also/gone", transcript_path="")
        _, cwd = _resume_command_for(s)
        assert cwd == str(Path.home())

    def test_resume_skips_a_deleted_project_path_for_a_live_cwd(self, tmp_path):
        from claude_monitor import _resume_command_for
        s = make_session(session_id="a", project_path="/definitely/not/here",
                         cwd=str(tmp_path), transcript_path="")
        _, cwd = _resume_command_for(s)
        assert cwd == str(tmp_path)


class TestJumpFindsBackgroundTabsOnEverySpace:
    """Max, 2026-08-27: "can't jump to existing one despite row in monitor
    existing." The matcher had two discovery paths and a blind spot
    between them: Ghostty's `windows.name()` reports each window's ACTIVE
    tab only, and the System Events walk lists only the CURRENT Space's
    windows. A background tab in a window on another Space was therefore
    invisible to both, and the Window-menu raise (which does see every
    window) only ran AFTER a window had already been identified, so it
    could never rescue the miss. Measured that day: the Ghostty-native
    walk saw 14 tabs where System Events saw 3.

    The JXA is not unit-testable on its own, so this asserts the shape of
    the script the matcher actually sends: a Ghostty-native tab walk that
    runs BEFORE the System Events fallback, with the same typing guard."""

    def _script_for(self, session):
        from unittest.mock import patch, MagicMock
        seen = {}

        def fake_run(argv, **kw):
            seen["script"] = argv[-1]
            r = MagicMock()
            r.stdout, r.stderr, r.returncode = "no_match", "", 0
            return r

        with patch("claude_monitor.subprocess.run", side_effect=fake_run):
            claude_monitor.focus_terminal_session(session)
        return seen["script"]

    def test_walks_ghostty_own_tabs_not_only_system_events(self):
        s = make_session(session_id="07a7b852-17f0-0000-0000-000000000000", title="frontier-research")
        js = self._script_for(s)
        assert "gh.windows()" in js and "w.tabs()" in js, "no Ghostty-native tab walk"

    def test_native_walk_precedes_the_system_events_fallback(self):
        """Order matters: System Events cannot see other Spaces, so the
        native walk must get first refusal."""
        s = make_session(session_id="07a7b852-17f0-0000-0000-000000000000", title="frontier-research")
        js = self._script_for(s)
        assert js.index("gh.windows()") < js.index("tabGroups[0].radioButtons()")

    def test_native_walk_keeps_the_wrong_tab_typing_guard(self):
        """Typing into the wrong tab is unrecoverable, so a send only fires
        when exactly one tab carries the sid marker."""
        s = make_session(session_id="07a7b852-17f0-0000-0000-000000000000", title="frontier-research")
        js = self._script_for(s)
        native = js[js.index("gh.windows()"):js.index("tabGroups[0].radioButtons()")]
        assert "abort_tab_type" in native
        assert "sidHits.length !== 1" in native

    def test_native_walk_matches_on_the_sid_marker(self):
        s = make_session(session_id="07a7b852-17f0-0000-0000-000000000000", title="frontier-research")
        js = self._script_for(s)
        assert "07a7b852" in js
        assert "tt.includes(sid8)" in js
