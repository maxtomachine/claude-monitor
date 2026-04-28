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
    KanbanView,
    TimelineView,
    StatsBar,
    Session,
    ALL_COLUMNS,
)
from tests.helpers import make_session


def _mock_sessions(sessions: list[Session]):
    """Return a patch that makes parse_sessions() return the given sessions.
    Also disables grouped view (the production default) so tests that don't
    test grouping see flat rows."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        with patch("claude_monitor.parse_sessions", return_value=sessions):
            # Patch the reactive default so new app instances start ungrouped
            original = ClaudeMonitor.show_groups._default
            ClaudeMonitor.show_groups._default = False
            try:
                yield
            finally:
                ClaudeMonitor.show_groups._default = original

    return _ctx()


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
                # Grouped view (default) adds group header rows
                assert table.row_count >= 3

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
                await pilot.press("v")
                await pilot.pause()

    async def test_refresh_keybinding(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("r")
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count >= 3


class TestSessionMenu:
    async def test_enter_opens_menu(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert len(pilot.app.screen_stack) > 1

    async def test_single_click_highlights_only(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                # click row index 1 (offset y=2: y=0 header, y=1 row0, y=2 row1)
                await pilot.click("#session-table", offset=(2, 2), times=1)
                await pilot.pause()
                assert table.cursor_row == 1
                assert len(pilot.app.screen_stack) == 1  # no menu

    async def test_double_click_jumps(self, sample_sessions):
        with _mock_sessions(sample_sessions), \
             patch("claude_monitor.focus_terminal_session", return_value=True) as mock_jump:
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.click("#session-table", offset=(2, 2), times=2)
                await pilot.pause()
                assert mock_jump.called
                assert len(pilot.app.screen_stack) == 1  # no menu

    async def test_double_click_on_highlighted_row_jumps(self, sample_sessions):
        """Regression: dbl-click on the already-selected row must not open
        the menu on the first click."""
        with _mock_sessions(sample_sessions), \
             patch("claude_monitor.focus_terminal_session", return_value=True) as mock_jump:
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                # row 0 is highlighted on mount
                await pilot.click("#session-table", offset=(2, 1), times=2)
                await pilot.pause()
                assert mock_jump.called
                assert len(pilot.app.screen_stack) == 1

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
                assert table.row_count >= 3

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
                await pilot.press("h")
                await pilot.pause()
                assert pilot.app.show_archived is True
                await pilot.press("h")
                await pilot.pause()
                assert pilot.app.show_archived is False

    async def test_archived_sessions_appear_when_toggled(self):
        active = make_session(session_id="active-1", title="Active")
        archived = make_session(session_id="old-1", title="Old Session", status="archived")
        all_sessions = [active, archived]
        active_only = [active]

        def _mock_parse(**kwargs):
            # side_effect (not _mock_sessions) because we return different data
            # depending on include_archived
            if kwargs.get("include_archived"):
                return all_sessions
            return active_only

        with patch("claude_monitor.parse_sessions", side_effect=_mock_parse):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                before = table.row_count
                await pilot.press("h")
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count > before  # archived session appeared

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
                assert "edit_name" in option_ids
                assert "resume" in option_ids


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


class TestKanban:
    async def test_kanban_opens_and_closes(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("v")
                await pilot.pause()
                assert isinstance(pilot.app.screen, KanbanView)
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(pilot.app.screen, KanbanView)

    async def test_kanban_shows_columns(self):
        sessions = [
            make_session(session_id="w1", title="Worker", status="working"),
            make_session(session_id="i1", title="Idler", status="idle"),
        ]
        with _mock_sessions(sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("v")
                await pilot.pause()
                screen = pilot.app.screen
                cards = screen.query(".kanban-card")
                assert len(cards) == 2

    async def test_kanban_excludes_subagents(self):
        sub = make_session(session_id="sub-1", is_subagent=True, parent_id="p1")
        parent = make_session(session_id="p1", title="Parent", subagents=[sub])
        with _mock_sessions([parent]):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("v")
                await pilot.pause()
                cards = pilot.app.screen.query(".kanban-card")
                assert len(cards) == 1

    async def test_kanban_arrow_navigation(self):
        sessions = [
            make_session(session_id="w1", title="W1", status="working"),
            make_session(session_id="w2", title="W2", status="working"),
            make_session(session_id="i1", title="I1", status="idle"),
        ]
        with _mock_sessions(sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("v")
                await pilot.pause()
                screen = pilot.app.screen
                start_col = screen._col
                assert screen._row == 0
                # Right jumps to next non-empty column
                await pilot.press("right")
                await pilot.pause()
                assert screen._col != start_col
                # Row clamped to new column's length
                assert screen._row < len(screen._grid[screen._col])

    async def test_kanban_enter_opens_session_menu(self):
        s = make_session(session_id="w1", title="Worker", status="working")
        with _mock_sessions([s]):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("v")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                # SessionMenu opens on top; KanbanView still underneath
                assert isinstance(pilot.app.screen, SessionMenu)
                assert any(isinstance(sc, KanbanView) for sc in pilot.app.screen_stack)
                # Escape → back to kanban
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(pilot.app.screen, KanbanView)


class TestEmptyState:
    async def test_mount_with_no_sessions_does_not_crash(self):
        # Regression: cursor_row is -1 on an empty DataTable; must not index
        # into _row_map / old_map with that.
        with patch("claude_monitor.parse_sessions", return_value=[]):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                # Explicitly drive a refresh cycle too
                pilot.app.refresh_sessions()
                await pilot.pause()
                assert pilot.app._row_map == []


class TestProactiveGroup:
    @pytest.fixture
    def grouped_sessions(self):
        return [
            make_session(session_id="bash-1", title="bashing-alpha", status="idle"),
            make_session(session_id="bash-2", title="bashing-beta", status="working"),
            make_session(session_id="bash-3", title="bashing-gamma", status="waiting"),
            make_session(session_id="other-1", title="other-thing", status="idle"),
        ]

    async def test_resolve_cursor_group_on_session(self, grouped_sessions):
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                # Find a bashing-* session row (skip group header)
                for i, s in enumerate(pilot.app._row_map):
                    if s and s.title.startswith("bashing-"):
                        table.move_cursor(row=i)
                        break
                gk, members = pilot.app._resolve_cursor_group()
                assert gk == "bashing"
                assert {m.session_id for m in members} == {"bash-1", "bash-2", "bash-3"}

    async def test_resolve_cursor_group_on_header(self, grouped_sessions):
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                # Find the bashing group header row (None in _row_map)
                for i, s in enumerate(pilot.app._row_map):
                    if s is None and i + 1 < len(pilot.app._row_map):
                        nxt = pilot.app._row_map[i + 1]
                        if nxt and nxt.title.startswith("bashing-"):
                            table.move_cursor(row=i)
                            break
                gk, members = pilot.app._resolve_cursor_group()
                assert gk == "bashing"
                assert len(members) == 3

    async def test_P_broadcasts_to_group(self, grouped_sessions):
        captured = {}
        def fake_broadcast(self, sessions, cmd, group):
            captured["sessions"] = [s.session_id for s in sessions]
            captured["cmd"] = cmd
            captured["group"] = group
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch.object(ClaudeMonitor, "_broadcast_command", fake_broadcast):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                for i, s in enumerate(pilot.app._row_map):
                    if s and s.title.startswith("bashing-"):
                        table.move_cursor(row=i)
                        break
                await pilot.press("P")
                await pilot.pause()
                # Worker runs in thread; give it a beat
                await pilot.app.workers.wait_for_complete()
                assert captured.get("cmd") == "/proactive"
                assert captured.get("group") == "bashing"
                assert set(captured.get("sessions", [])) == {"bash-1", "bash-2", "bash-3"}

    async def test_P_refuses_ungrouped(self, grouped_sessions):
        # Only one "other-*" session → singleton → ungrouped bucket
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch.object(ClaudeMonitor, "_broadcast_command") as mock_bc:
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                for i, s in enumerate(pilot.app._row_map):
                    if s and s.session_id == "other-1":
                        table.move_cursor(row=i)
                        break
                await pilot.press("P")
                await pilot.pause()
                mock_bc.assert_not_called()


class TestTimeline:
    async def test_timeline_opens_and_closes(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                # v once → kanban, v again → timeline
                await pilot.press("v")
                await pilot.pause()
                assert isinstance(pilot.app.screen, KanbanView)
                await pilot.press("v")
                await pilot.pause()
                assert isinstance(pilot.app.screen, TimelineView)
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(pilot.app.screen, TimelineView)

    async def test_timeline_shows_bars(self):
        sessions = [
            make_session(session_id="w1", title="Worker", status="working"),
            make_session(session_id="i1", title="Idler", status="idle"),
        ]
        with _mock_sessions(sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                await pilot.press("v")
                await pilot.pause()
                await pilot.press("v")
                await pilot.pause()
                bars = pilot.app.screen.query(".timeline-bar")
                assert len(bars) == 2

    async def test_view_cycle_full_loop(self, sample_sessions):
        """v cycles: rows → kanban → timeline → rows."""
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                # Start in rows
                assert not isinstance(pilot.app.screen, (KanbanView, TimelineView))
                # v → kanban
                await pilot.press("v")
                await pilot.pause()
                assert isinstance(pilot.app.screen, KanbanView)
                # v → timeline
                await pilot.press("v")
                await pilot.pause()
                assert isinstance(pilot.app.screen, TimelineView)
                # v → rows (back to default)
                await pilot.press("v")
                await pilot.pause()
                assert not isinstance(pilot.app.screen, (KanbanView, TimelineView))


@pytest.fixture
def archived_sessions():
    return [
        make_session(session_id="arch-1", title="Old A", status="archived"),
        make_session(session_id="arch-2", title="Old B", status="archived"),
        make_session(session_id="arch-3", title="Old C", status="closed"),
        make_session(session_id="live-1", title="Zlive", status="working"),
    ]


class TestHideAndMultiSelect:
    async def _app(self, sessions):
        """Mount app in history mode with hidden-set persistence mocked."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx():
            saved = []
            with _mock_sessions(sessions), \
                 patch("claude_monitor.load_hidden_sessions", return_value=set()), \
                 patch("claude_monitor.save_hidden_sessions",
                       side_effect=lambda h: saved.append(set(h))):
                async with ClaudeMonitor().run_test() as pilot:
                    pilot.app.show_archived = True
                    await pilot.pause()
                    pilot._saved = saved  # type: ignore
                    yield pilot
        return _ctx()

    async def test_delete_requires_history_mode(self, archived_sessions):
        with _mock_sessions(archived_sessions), \
             patch("claude_monitor.load_hidden_sessions", return_value=set()):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                # show_archived defaults False
                await pilot.press("backspace")
                await pilot.pause()
                assert pilot.app._delete_armed_for is None

    async def test_delete_arms_then_hides(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)  # arch-1
            await pilot.pause()
            await pilot.press("backspace")
            await pilot.pause()
            assert pilot.app._delete_armed_for == frozenset({"arch-1"})
            assert "arch-1" not in pilot.app._hidden
            await pilot.press("backspace")
            await pilot.pause()
            assert "arch-1" in pilot.app._hidden
            assert pilot._saved[-1] == {"arch-1"}

    async def test_delete_refuses_live_session(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            # find live-1's row
            for i, s in enumerate(pilot.app._row_map):
                if s and s.session_id == "live-1":
                    table.move_cursor(row=i)
                    break
            await pilot.pause()
            await pilot.press("backspace")
            await pilot.pause()
            assert pilot.app._delete_armed_for is None
            assert "live-1" not in pilot.app._hidden

    async def test_shift_down_extends_selection(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("shift+down")
            await pilot.pause()
            assert len(pilot.app._selection) == 2
            await pilot.press("shift+down")
            await pilot.pause()
            assert len(pilot.app._selection) == 3

    async def test_plain_arrow_clears_selection(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("shift+down")
            await pilot.pause()
            assert pilot.app._selection
            await pilot.press("down")
            await pilot.pause()
            assert pilot.app._selection == set()

    async def test_batch_hide_via_selection(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("shift+down", "shift+down")  # select 3 rows
            await pilot.pause()
            await pilot.press("backspace")  # arm
            await pilot.pause()
            armed = pilot.app._delete_armed_for
            assert armed is not None and len(armed) >= 2
            await pilot.press("backspace")  # confirm
            await pilot.pause()
            assert pilot.app._hidden >= {"arch-1", "arch-2"}

    async def test_hide_preserves_cursor_at_survivor(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            # cursor on row 1 (arch-2)
            pilot.app._extending_cursor = True
            table.move_cursor(row=1)
            pilot.app._extending_cursor = False
            await pilot.pause()
            await pilot.press("backspace", "backspace")
            await pilot.pause()
            assert "arch-2" in pilot.app._hidden
            # arch-2 gone; cursor should land on next survivor (arch-3), not row 0
            cur = pilot.app._row_map[table.cursor_row]
            assert cur is not None
            assert cur.session_id != "arch-1"  # not reset to top

    async def test_cursor_move_disarms_delete(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("backspace")
            await pilot.pause()
            assert pilot.app._delete_armed_for is not None
            await pilot.press("down")
            await pilot.pause()
            assert pilot.app._delete_armed_for is None
