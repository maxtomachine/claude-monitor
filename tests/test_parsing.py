"""Tests for transcript parsing and session building."""

import json
import time

from claude_monitor import (
    parse_timestamp,
    scan_full_file,
    estimate_cost,
    determine_status,
    sort_sessions,
    SortMode,
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
    def test_recent_activity_is_working(self):
        assert determine_status("test-id", time.time() - 5) == "working"

    def test_moderate_elapsed_is_waiting(self):
        assert determine_status("test-id", time.time() - 60) == "waiting"

    def test_old_activity_is_idle(self):
        assert determine_status("test-id", time.time() - 600) == "idle"

    def test_no_activity(self):
        assert determine_status("test-id", 0) == "idle"


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
