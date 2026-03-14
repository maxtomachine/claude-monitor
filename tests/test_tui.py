"""TUI integration tests using Textual's async test framework.

These tests mount the actual app headlessly and simulate keypresses to verify
that UI interactions work correctly — no real terminal needed.
"""

from unittest.mock import patch

import pytest

from textual.widgets import DataTable, OptionList, Input, Static

from claude_monitor import (
    ClaudeMonitor,
    SessionMenu,
    ColumnPicker,
    StatsBar,
    Session,
    ALL_COLUMNS,
)
from tests.helpers import make_session


def _mock_sessions(sessions: list[Session]):
    """Return a patch that makes parse_sessions() return the given sessions."""
    return patch("claude_monitor.parse_sessions", return_value=sessions)


@pytest.fixture
def sample_sessions():
    return [
        make_session(session_id="sess-1", title="First Session", status="working",
                     cost=2.50, tokens_in=50_000, tokens_out=10_000, context_pct=70),
        make_session(session_id="sess-2", title="Second Session", status="idle",
                     cost=1.00, tokens_in=20_000, tokens_out=5_000, context_pct=90),
        make_session(session_id="sess-3", title="Third Session", status="waiting",
                     cost=5.00, tokens_in=100_000, tokens_out=30_000, context_pct=30),
    ]


class TestAppMounts:
    async def test_app_starts(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                assert table is not None
                assert table.row_count == 3

    async def test_stats_bar_shows(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                stats = pilot.app.query_one(StatsBar)
                assert stats is not None

    async def test_detail_panel_exists(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                panel = pilot.app.query_one("#detail-panel", Static)
                assert panel is not None


class TestKeyBindings:
    async def test_sort_cycles(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                initial_sort = pilot.app.sort_mode
                await pilot.press("s")
                await pilot.pause()
                assert pilot.app.sort_mode != initial_sort

    async def test_toggle_subagents(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                assert pilot.app.show_subagents is False
                await pilot.press("a")
                await pilot.pause()
                assert pilot.app.show_subagents is True
                await pilot.press("a")
                await pilot.pause()
                assert pilot.app.show_subagents is False

    async def test_search_opens_and_closes(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                search = pilot.app.query_one("#search-bar", Input)
                assert search.display is False
                await pilot.press("slash")
                await pilot.pause()
                assert search.display is True
                await pilot.press("escape")
                await pilot.pause()
                assert search.display is False

    async def test_vim_navigation(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("j")
                await pilot.press("j")
                await pilot.press("k")
                await pilot.pause()

    async def test_refresh_keybinding(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("r")
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 3


class TestSessionMenu:
    async def test_enter_opens_menu(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert len(pilot.app.screen_stack) > 1

    async def test_menu_shows_options(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                assert options is not None
                assert options.option_count >= 5

    async def test_menu_escape_closes(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert len(pilot.app.screen_stack) > 1
                await pilot.press("escape")
                await pilot.pause()
                assert len(pilot.app.screen_stack) == 1

    async def test_menu_shows_session_title(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                screen = pilot.app.screen
                title_label = screen.query_one("#menu-title")
                label_text = str(title_label.render())
                # Menu title should match whichever session is highlighted
                session_titles = [s.title for s in sample_sessions]
                assert any(t in label_text for t in session_titles)

    async def test_menu_has_remote_link_when_available(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                option_ids = [options.get_option_at_index(i).id
                              for i in range(options.option_count)]
                assert "remote" in option_ids

    async def test_menu_no_remote_when_absent(self):
        sessions = [make_session(session_id="no-remote", remote_url="", slug="")]
        with _mock_sessions(sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                option_ids = [options.get_option_at_index(i).id
                              for i in range(options.option_count)]
                assert "remote" not in option_ids


class TestColumnPicker:
    async def test_column_picker_opens(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("c")
                await pilot.pause()
                assert len(pilot.app.screen_stack) > 1

    async def test_column_picker_escape_closes(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("c")
                await pilot.pause()
                assert len(pilot.app.screen_stack) > 1
                await pilot.press("escape")
                await pilot.pause()
                assert len(pilot.app.screen_stack) == 1

    async def test_column_toggle(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("c")
                await pilot.pause()
                screen = pilot.app.screen
                picker = screen
                ol = screen.query_one("#picker-list", OptionList)
                first_key = picker._col_keys[0]
                was_selected = first_key in picker.selected_cols
                await pilot.press("enter")
                await pilot.pause()
                assert (first_key in picker.selected_cols) != was_selected


class TestSearch:
    async def test_search_filters_sessions(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("slash")
                await pilot.pause()
                search = pilot.app.query_one("#search-bar", Input)
                search.value = "First"
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 1

    async def test_clear_search_restores_all(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("slash")
                await pilot.pause()
                search = pilot.app.query_one("#search-bar", Input)
                search.value = "First"
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 3

    async def test_search_no_match(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("slash")
                await pilot.pause()
                search = pilot.app.query_one("#search-bar", Input)
                search.value = "nonexistent-session-xyz"
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 0


class TestArchived:
    async def test_archive_toggle(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                assert pilot.app.show_archived is False
                await pilot.press("z")
                await pilot.pause()
                assert pilot.app.show_archived is True
                await pilot.press("z")
                await pilot.pause()
                assert pilot.app.show_archived is False

    async def test_archived_menu_shows_resume(self):
        s = make_session(session_id="old-1", title="Old Session", status="archived")
        with _mock_sessions([s]):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                option_ids = [options.get_option_at_index(i).id
                              for i in range(options.option_count)]
                assert "resume" in option_ids
                assert "jump" not in option_ids

    async def test_active_menu_shows_jump(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                option_ids = [options.get_option_at_index(i).id
                              for i in range(options.option_count)]
                assert "jump" in option_ids
                assert "resume" not in option_ids


class TestSubagents:
    async def test_subagents_shown_when_toggled(self):
        sub = make_session(session_id="sub-1", title="agent-1", is_subagent=True,
                           parent_id="parent-1")
        parent = make_session(session_id="parent-1", title="Parent", subagents=[sub])
        with _mock_sessions([parent]):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 1
                await pilot.press("a")
                await pilot.pause()
                assert table.row_count == 2

    async def test_subagents_hidden_again(self):
        sub = make_session(session_id="sub-1", is_subagent=True, parent_id="p1")
        parent = make_session(session_id="p1", subagents=[sub])
        with _mock_sessions([parent]):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("a")
                await pilot.pause()
                await pilot.press("a")
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 1
