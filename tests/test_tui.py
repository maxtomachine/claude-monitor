"""TUI integration tests using Textual's async test framework.

These tests mount the actual app headlessly and simulate keypresses to verify
that UI interactions work correctly — no real terminal needed.
"""

from unittest.mock import patch

import time

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


class TestSessionColumnElastic:
    async def _session_w(self, sessions, term_w):
        with _mock_sessions(sessions):
            async with ClaudeMonitor().run_test(size=(term_w, 30)) as pilot:
                await pilot.pause()
                table = pilot.app.query_one("#session-table", DataTable)
                col = table.columns["session"]
                others = sum(
                    c.get_render_width(table)
                    for k, c in table.columns.items()
                    if getattr(k, "value", k) != "session"
                )
                return col.width, others, table.cell_padding

    async def test_session_absorbs_slack(self, sample_sessions):
        w, others, pad = await self._session_w(sample_sessions, 160)
        assert w == max(20, 160 - others - 2 * pad)
        assert w > 160 // 3  # not the old 1/3 cap

    async def test_session_shrinks_last_on_narrow(self, sample_sessions):
        wide, others_w, _ = await self._session_w(sample_sessions, 160)
        narrow, others_n, _ = await self._session_w(sample_sessions, 110)
        # Other columns are content-sized so unchanged; session gives the full 50
        assert others_w == others_n
        assert wide - narrow == 50
        assert narrow >= 20


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

    async def test_letter_keys_type_into_search_not_hotkeys(self, sample_sessions):
        """While the search box has focus, single-letter app hotkeys (s, r, q,
        ...) are filter text, not actions."""
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                sort_before = pilot.app.sort_mode
                await pilot.press("slash")
                await pilot.pause()
                await pilot.press("s")
                await pilot.pause()
                search = pilot.app.query_one("#search-bar", Input)
                assert search.value == "s"
                assert pilot.app.sort_mode == sort_before

    async def test_down_from_search_drops_to_table_keeping_filter(
        self, sample_sessions
    ):
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
                await pilot.press("down")
                await pilot.pause()
                assert table.has_focus
                assert pilot.app._filter == "First"
                assert table.row_count == 1
                assert search.display is True
                # Esc from the table still clears the filter and restores rows.
                await pilot.press("escape")
                await pilot.pause()
                assert pilot.app._filter == ""
                assert search.display is False
                assert table.row_count >= 3

    async def test_jump_clears_search(self, sample_sessions):
        """A successful jump-to-terminal exits search mode so the full table
        is showing when the user comes back to the monitor. Asserted by
        checking the handler invokes action_clear_search; the dismiss path
        itself is covered by test_clear_search_restores_all."""
        with _mock_sessions(sample_sessions), \
             patch("claude_monitor.focus_terminal_session", return_value=True), \
             patch.object(ClaudeMonitor, "action_clear_search") as clear:
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                s = pilot.app._flat_rows[0]
                pilot.app._make_menu_handler(s)("jump")
                clear.assert_called_once()

    async def test_failed_jump_keeps_search(self, sample_sessions):
        with _mock_sessions(sample_sessions), \
             patch("claude_monitor.focus_terminal_session", return_value=False), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor._heal_hook_state"), \
             patch("claude_monitor._resolve_match_candidates", return_value=[]), \
             patch.object(ClaudeMonitor, "action_clear_search") as clear:
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                s = pilot.app._flat_rows[0]
                pilot.app._make_menu_handler(s)("jump")
                clear.assert_not_called()

    async def test_i_toggles_detail_panel(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                panel = pilot.app.query_one("#detail-panel", Static)
                assert panel.display is True
                await pilot.press("i")
                await pilot.pause()
                assert panel.display is False
                assert pilot.app.show_detail is False
                await pilot.press("i")
                await pilot.pause()
                assert panel.display is True


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


class TestTitleDisambiguation:
    async def test_duplicate_titles_get_sid_suffix(self):
        dups = [
            make_session(session_id="abc12345-x", title="Shared Name", status="idle"),
            make_session(session_id="def67890-y", title="Shared Name", status="working"),
            make_session(session_id="zzz99999-z", title="Unique", status="idle"),
        ]
        with _mock_sessions(dups):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                titles = [s.title for s in pilot.app._flat_rows]
                assert "Shared Name ·abc12345" in titles
                assert "Shared Name ·def67890" in titles
                assert "Unique" in titles


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

    async def test_delete_works_outside_history_mode(self, archived_sessions):
        with _mock_sessions(archived_sessions), \
             patch("claude_monitor.load_hidden_sessions", return_value=set()), \
             patch("claude_monitor.save_hidden_sessions"):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                # show_archived defaults False; cursor on archived row 0
                await pilot.press("backspace")
                await pilot.pause()
                assert pilot.app._delete_armed_for is not None

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

    async def test_shift_up_shrinks_selection(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("shift+down", "shift+down")
            await pilot.pause()
            n_before = len(pilot.app._selection)
            await pilot.press("shift+up")
            await pilot.pause()
            assert len(pilot.app._selection) == n_before - 1
            assert pilot.app._selection_anchor is not None

    async def test_selection_survives_refresh(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            pilot.app._extending_cursor = True
            table.move_cursor(row=1)  # anchor NOT at row 0
            pilot.app._extending_cursor = False
            await pilot.pause()
            await pilot.press("shift+down")
            await pilot.pause()
            sel_before = set(pilot.app._selection)
            anchor_before = pilot.app._selection_anchor
            pilot.app.refresh_sessions()
            await pilot.pause()
            await pilot.pause()
            assert pilot.app._selection == sel_before
            assert pilot.app._selection_anchor == anchor_before

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


class TestBell:
    async def test_no_bell_on_startup(self):
        sessions = [make_session(session_id="s1", title="t", status="waiting")]
        with _mock_sessions(sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                assert pilot.app._bell == {}

    async def test_rings_on_working_to_waiting(self):
        s = make_session(session_id="s1", title="t", status="working")
        with patch("claude_monitor.parse_sessions", side_effect=[[s], [s], [s]]):
            original = ClaudeMonitor.show_groups._default
            ClaudeMonitor.show_groups._default = False
            try:
                async with ClaudeMonitor().run_test() as pilot:
                    await pilot.pause()
                    assert pilot.app._bell == {}
                    s.status = "waiting"
                    pilot.app.refresh_sessions()
                    await pilot.pause()
                    assert "s1" in pilot.app._bell
                    assert pilot.app._bell["s1"]["acked"] is False
            finally:
                ClaudeMonitor.show_groups._default = original

    async def test_ack_clears_on_next_tick(self):
        s = make_session(session_id="s1", title="t", status="working")
        with patch("claude_monitor.parse_sessions", side_effect=[[s], [s], [s]]):
            original = ClaudeMonitor.show_groups._default
            ClaudeMonitor.show_groups._default = False
            try:
                async with ClaudeMonitor().run_test() as pilot:
                    await pilot.pause()
                    s.status = "waiting"
                    pilot.app.refresh_sessions()
                    await pilot.pause()
                    assert "s1" in pilot.app._bell
                    pilot.app._ack_bell("s1")
                    pilot.app._tick_bell()
                    assert "s1" not in pilot.app._bell
            finally:
                ClaudeMonitor.show_groups._default = original

    async def test_working_again_silences(self):
        s = make_session(session_id="s1", title="t", status="working")
        with patch("claude_monitor.parse_sessions", side_effect=[[s], [s], [s], [s]]):
            original = ClaudeMonitor.show_groups._default
            ClaudeMonitor.show_groups._default = False
            try:
                async with ClaudeMonitor().run_test() as pilot:
                    await pilot.pause()
                    s.status = "waiting"
                    pilot.app.refresh_sessions()
                    await pilot.pause()
                    assert "s1" in pilot.app._bell
                    s.status = "working"
                    pilot.app.refresh_sessions()
                    await pilot.pause()
                    assert "s1" not in pilot.app._bell
            finally:
                ClaudeMonitor.show_groups._default = original

    async def test_compose_first_cell_precedence(self):
        with _mock_sessions([]):
            async with ClaudeMonitor().run_test() as pilot:
                app = pilot.app
                s = make_session(session_id="s1", title="t", is_scheduled=True)
                app._pinned = {"s1"}
                app._bell = {"s1": {"rang_at": time.time(), "acked": False}}
                app._pulse_phase = True
                assert "●" in app._compose_first_cell(s, "  base")
                app._bell["s1"]["acked"] = True
                assert "⊙" in app._compose_first_cell(s, "  base")
                app._pinned = set()
                assert "↻" in app._compose_first_cell(s, "  base")
                s2 = make_session(session_id="s2", title="t2")
                assert app._compose_first_cell(s2, "  base") == "  base"


class TestRestartHardening:
    async def test_restart_survives_git_pull_failure(self, sample_sessions):
        """R (restart) must not crash the app if `git pull` fails: missing repo
        dir (a worktree removed under a running instance), git off PATH, offline,
        or timeout. It should swallow the error and still exit with the restart
        code. Regression: a deleted worktree cwd raised an unhandled
        FileNotFoundError and took the whole TUI down."""
        from claude_monitor import RESTART_EXIT_CODE
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await pilot.pause()
                with patch.object(pilot.app, "_save_view_state"), \
                     patch("claude_monitor.subprocess.run",
                           side_effect=OSError(2, "No such file or directory")):
                    pilot.app.action_restart()  # must not raise
                    await pilot.pause()
                assert pilot.app.return_code == RESTART_EXIT_CODE
