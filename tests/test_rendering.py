"""Tests for row rendering and column logic."""

from claude_monitor import (
    render_row,
    ALL_COLUMNS,
    get_visible_columns,
    get_column_order,
    DOING_MAX_WIDTH,
    Task,
    format_plan,
)
from tests.helpers import make_session


class TestRenderRow:
    def test_all_default_columns(self):
        s = make_session()
        cols = [k for k, v in ALL_COLUMNS.items() if v["default"]]
        cells = render_row(s, cols)
        assert len(cells) == len(cols)

    def test_status_column(self):
        s = make_session(status="working")
        cells = render_row(s, ["status"])
        assert "WORKING" in cells[0]
        assert "green" in cells[0]

    def test_idle_status(self):
        s = make_session(status="idle")
        cells = render_row(s, ["status"])
        assert "IDLE" in cells[0]

    def test_session_title(self):
        s = make_session(title="My Session")
        cells = render_row(s, ["session"])
        assert "My Session" in cells[0]

    def test_subagent_prefix(self):
        s = make_session(is_subagent=True, title="agent-1")
        cells = render_row(s, ["session"])
        assert "└─" in cells[0]

    def test_session_with_subagents_count(self):
        sub = make_session(session_id="sub1", is_subagent=True)
        s = make_session(subagents=[sub])
        cells = render_row(s, ["session"])
        assert "(+1)" in cells[0]

    def test_cost_column(self):
        s = make_session(cost=5.25)
        cells = render_row(s, ["cost"])
        assert "$5.25" in cells[0]

    def test_zero_cost(self):
        s = make_session(cost=0)
        cells = render_row(s, ["cost"])
        assert "—" in cells[0]

    def test_tokens_column(self):
        s = make_session(tokens_in=50_000, tokens_out=10_000)
        cells = render_row(s, ["tokens"])
        assert "10k" in cells[0]

    def test_context_column(self):
        s = make_session(context_pct=75)
        cells = render_row(s, ["context"])
        assert "75%" in cells[0]

    def test_doing_column_truncation(self):
        s = make_session(status="working", last_tool="Read",
                         last_tool_input={"file_path": "/very/long/path/to/some/deeply/nested/file.py"})
        cells = render_row(s, ["doing"])
        # Should be truncated to DOING_MAX_WIDTH
        # Strip Rich markup to check actual text length
        import re
        plain = re.sub(r'\[.*?\]', '', cells[0]).replace("\\[", "[").replace("\\]", "]")
        assert len(plain) <= DOING_MAX_WIDTH

    def test_doing_idle_is_dim(self):
        s = make_session(status="idle", last_tool="Read",
                         last_tool_input={"file_path": "/tmp/x.py"})
        cells = render_row(s, ["doing"])
        assert "[dim]" in cells[0]

    def test_doing_needs_approval_is_yellow(self):
        s = make_session(status="needs_approval", last_tool="Bash",
                         last_tool_input={"command": "rm -rf /tmp"})
        cells = render_row(s, ["doing"])
        assert "[yellow]" in cells[0]

    def test_doing_no_activity(self):
        s = make_session(status="working", last_tool="", last_assistant_text="")
        cells = render_row(s, ["doing"])
        assert "—" in cells[0]

    def test_mcp_column(self):
        s = make_session(mcp_calls=5)
        cells = render_row(s, ["mcp"])
        assert "5" in cells[0]

    def test_mcp_zero(self):
        s = make_session(mcp_calls=0)
        cells = render_row(s, ["mcp"])
        assert "—" in cells[0]

    def test_compact_column(self):
        s = make_session(compact_count=3)
        cells = render_row(s, ["compact"])
        assert "✻" in cells[0]

    def test_project_hidden_for_subagent(self):
        s = make_session(is_subagent=True, project="myproject")
        cells = render_row(s, ["project"])
        assert cells[0] == ""

    def test_msgs_hidden_for_subagent(self):
        s = make_session(is_subagent=True, message_count=10)
        cells = render_row(s, ["msgs"])
        assert cells[0] == ""


class TestColumnConfig:
    def test_default_columns_exist(self):
        defaults = get_visible_columns()
        assert len(defaults) > 0
        for col in defaults:
            assert col in ALL_COLUMNS

    def test_column_order_has_all(self):
        order = get_column_order()
        assert set(order) == set(ALL_COLUMNS.keys())


class TestFormatPlan:
    def test_empty_tasks(self):
        assert format_plan([]) == ""

    def test_all_pending(self):
        tasks = [
            Task(id="1", subject="Step one", status="pending"),
            Task(id="2", subject="Step two", status="pending"),
        ]
        result = format_plan(tasks)
        assert "0/2 done" in result
        assert "○" in result

    def test_partial_progress(self):
        tasks = [
            Task(id="1", subject="Done step", status="completed"),
            Task(id="2", subject="Current step", status="in_progress", active_form="Doing current"),
            Task(id="3", subject="Future step", status="pending"),
        ]
        result = format_plan(tasks)
        assert "1/3 done" in result
        assert "✓" in result
        assert "▸" in result
        assert "Doing current" in result

    def test_all_completed(self):
        tasks = [
            Task(id="1", subject="Step one", status="completed"),
            Task(id="2", subject="Step two", status="completed"),
        ]
        result = format_plan(tasks)
        assert "2/2 done" in result

    def test_truncation(self):
        tasks = [Task(id=str(i), subject=f"Step {i}", status="pending") for i in range(12)]
        result = format_plan(tasks, max_lines=5)
        assert "+7 more" in result

    def test_in_progress_shown_in_header(self):
        tasks = [
            Task(id="1", subject="Active task", status="in_progress", active_form="Working on it"),
        ]
        result = format_plan(tasks)
        assert "Working on it" in result
