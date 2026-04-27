"""Tests for gerund generation — the 'Doing' column logic."""

from claude_monitor import (
    _to_gerund,
    _gerund_from_tool,
    _gerund_from_text,
    _to_past_tense,
    generate_activity,
)
from tests.helpers import make_session


class TestToGerund:
    def test_basic_verb(self):
        assert _to_gerund("read") == "Reading"

    def test_verb_ending_in_e(self):
        assert _to_gerund("write") == "Writing"

    def test_verb_already_gerund(self):
        assert _to_gerund("running") == "Running"

    def test_verb_doubling_consonant(self):
        assert _to_gerund("run") == "Running"

    def test_verb_ending_in_ee(self):
        assert _to_gerund("see") == "Seeing"

    def test_longer_verb_no_double(self):
        assert _to_gerund("search") == "Searching"


class TestGerundFromTool:
    def test_bash_with_description(self):
        result = _gerund_from_tool("Bash", {"description": "Install deps", "command": "npm install"})
        assert result == "Install deps"

    def test_bash_without_description(self):
        result = _gerund_from_tool("Bash", {"command": "git status"})
        assert result == "Running git"

    def test_read_file(self):
        result = _gerund_from_tool("Read", {"file_path": "/tmp/config.py"})
        assert result == "Reading config.py"

    def test_edit_file(self):
        result = _gerund_from_tool("Edit", {"file_path": "/src/main.rs"})
        assert result == "Editing main.rs"

    def test_write_file(self):
        result = _gerund_from_tool("Write", {"file_path": "/new/file.txt"})
        assert result == "Writing file.txt"

    def test_grep(self):
        result = _gerund_from_tool("Grep", {"pattern": "TODO"})
        assert result == "Searching for 'TODO'"

    def test_glob(self):
        assert _gerund_from_tool("Glob", {}) == "Finding files"

    def test_agent(self):
        assert _gerund_from_tool("Agent", {}) == "Running subagent"

    def test_mcp_gmail_search(self):
        result = _gerund_from_tool("mcp__claude_ai_Google_Gmail_All_Access__search_emails", {})
        assert "Searching" in result
        assert "Gmail" in result

    def test_mcp_calendar_create(self):
        result = _gerund_from_tool("mcp__claude_ai_Google_Calendar_Edit__create_event", {})
        assert "Creating" in result
        assert "Calendar" in result

    def test_mcp_whoop_get_recovery(self):
        result = _gerund_from_tool("mcp__claude_ai_Whoop_MCP__whoop_get_recovery", {})
        assert "Fetching" in result
        assert "WHOOP" in result

    def test_mcp_monarch(self):
        result = _gerund_from_tool("mcp__claude_ai_Monarch_Money__list_accounts", {})
        assert "Listing" in result
        assert "Monarch" in result

    def test_unknown_tool(self):
        result = _gerund_from_tool("CustomTool", {})
        assert result == "Using CustomTool"

    def test_read_no_filepath(self):
        assert _gerund_from_tool("Read", {}) == "Reading"

    def test_bash_unknown_command(self):
        result = _gerund_from_tool("Bash", {"command": "whoami"})
        assert result == "Running command"


class TestGerundFromText:
    def test_starts_with_gerund(self):
        result = _gerund_from_text("Reading the configuration file now.")
        assert result is not None
        assert "Reading" in result

    def test_let_me(self):
        result = _gerund_from_text("Let me check the database connection.")
        assert result is not None
        assert "Checking" in result

    def test_ill(self):
        result = _gerund_from_text("I'll update the configuration.")
        assert result is not None
        assert "Updating" in result

    def test_im_going_to(self):
        result = _gerund_from_text("I'm going to fix the bug in auth.")
        assert result is not None
        assert "Fixing" in result

    def test_no_match(self):
        result = _gerund_from_text("The sky is blue today.")
        assert result is None

    def test_im_gerund(self):
        result = _gerund_from_text("I'm analyzing the test results now.")
        assert result is not None
        assert "analyzing" in result


class TestToPastTense:
    def test_known_gerund(self):
        assert _to_past_tense("Reading config.py") == "Read config.py"

    def test_editing(self):
        assert _to_past_tense("Editing main.rs") == "Edited main.rs"

    def test_unknown_gerund(self):
        result = _to_past_tense("Analyzing data")
        assert "Analyz" in result  # fallback: strip -ing, add -ed


class TestGenerateActivity:
    def test_working_session_with_tool(self):
        s = make_session(status="working", last_tool="Read",
                         last_tool_input={"file_path": "/tmp/foo.py"})
        result = generate_activity(s)
        assert result == "Reading foo.py"

    def test_idle_session_past_tense(self):
        s = make_session(status="idle", last_tool="Read",
                         last_tool_input={"file_path": "/tmp/foo.py"})
        result = generate_activity(s)
        assert result == "Read foo.py"

    def test_needs_approval(self):
        s = make_session(status="needs_approval", last_tool="Bash",
                         last_tool_input={"command": "rm -rf /tmp/old"})
        result = generate_activity(s)
        assert result == "Awaiting approval"

    def test_no_tool_no_text(self):
        s = make_session(status="working", last_tool="", last_assistant_text="")
        assert generate_activity(s) == ""

    def test_background_shows_agent_count(self):
        s = make_session(status="background", background_count=3,
                         last_tool="", last_assistant_text="")
        assert generate_activity(s) == "3 agents running"

    def test_background_singular(self):
        s = make_session(status="background", background_count=1)
        assert generate_activity(s) == "1 agent running"

    def test_fallback_to_text(self):
        s = make_session(status="working", last_tool="",
                         last_assistant_text="Let me check the logs.")
        result = generate_activity(s)
        assert result is not None
        assert "Checking" in result
