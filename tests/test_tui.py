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
from tests.helpers import make_session, settle


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
        make_session(session_id="sess-2", title="Second Session", status="done",
                     cost=1.00, tokens_in=20_000, tokens_out=5_000, context_pct=90),
        make_session(session_id="sess-3", title="Third Session", status="needs_approval",
                     cost=5.00, tokens_in=100_000, tokens_out=30_000, context_pct=30),
    ]


class TestAppMounts:
    async def test_app_starts(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                assert table is not None
                # Grouped view (default) adds group header rows
                assert table.row_count >= 3

    async def test_stats_bar_shows(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                stats = pilot.app.query_one(StatsBar)
                assert stats is not None

    async def test_stats_bar_ready_color_follows_theme(self, sample_sessions):
        """Bug reported by Max (2026-08-17): yellow doesn't pop against
        light mode's own cream background. The stats bar's "N ready" count
        must match whatever color the individual READY rows use for the
        current theme, not stay hardcoded to bright_yellow. update_stats()
        takes `dark` explicitly (the same value _refresh_apply's own
        sys_dark supplies to render_row()), not an independent self.app.
        theme check: two separately-derived light/dark sources were only
        coincidentally in sync (caught by review, 2026-08-17)."""
        from claude_monitor import SortMode, READY_COLOR_DARK, READY_COLOR_LIGHT
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                stats = pilot.app.query_one(StatsBar)
                label = stats.query_one("#stats-done")

                stats.update_stats(sample_sessions, SortMode.ALPHA, dark=True)
                styles = [span.style for span in label.render().spans]
                assert READY_COLOR_DARK in styles

                stats.update_stats(sample_sessions, SortMode.ALPHA, dark=False)
                styles = [span.style for span in label.render().spans]
                assert READY_COLOR_LIGHT in styles

    async def test_detail_panel_exists(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                panel = pilot.app.query_one("#detail-panel", Static)
                assert panel is not None


class TestSessionColumnElastic:
    async def _session_w(self, sessions, term_w):
        with _mock_sessions(sessions):
            async with ClaudeMonitor().run_test(size=(term_w, 30)) as pilot:
                await settle(pilot)
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
                await settle(pilot)
                initial_sort = pilot.app.sort_mode
                await pilot.press("ctrl+s")
                await settle(pilot)
                assert pilot.app.sort_mode != initial_sort

    async def test_toggle_subagents(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                assert pilot.app.show_subagents is False
                await pilot.press("ctrl+a")
                await settle(pilot)
                assert pilot.app.show_subagents is True
                await pilot.press("ctrl+a")
                await settle(pilot)
                assert pilot.app.show_subagents is False

    async def test_search_opens_and_closes(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                search = pilot.app.query_one("#search-bar", Input)
                assert search.display is False
                await pilot.press("slash")
                await settle(pilot)
                assert search.display is True
                await pilot.press("escape")
                await settle(pilot)
                assert search.display is False

    async def test_vim_navigation(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+j")
                await pilot.press("ctrl+j")
                await settle(pilot)

    async def test_ctrl_r_is_no_longer_bound(self, sample_sessions):
        """Max, 2026-08-18: "let's just replace normal refresh with shift r,
        we don't need the cruft." Ctrl+R was removed; R (Shift+r) is the
        one refresh key. The old test here pressed ctrl+r and passed
        vacuously (rows were already populated by mount's own refresh).
        Now assert the binding is actually gone. R itself is not pressed
        here: action_restart runs git pull and exits the app under test."""
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                bound_keys = {b.key for b in pilot.app.BINDINGS}
                assert "ctrl+r" not in bound_keys
                assert "R" in bound_keys
                r_binding = next(b for b in pilot.app.BINDINGS if b.key == "R")
                assert r_binding.action == "restart"
                assert r_binding.description == "Refresh"


class TestSessionMenu:
    async def test_enter_opens_menu(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                assert len(pilot.app.screen_stack) > 1

    async def test_single_click_highlights_only(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                # click row index 1 (offset y=2: y=0 header, y=1 row0, y=2 row1)
                await pilot.click("#session-table", offset=(2, 2), times=1)
                await settle(pilot)
                assert table.cursor_row == 1
                assert len(pilot.app.screen_stack) == 1  # no menu

    async def test_double_click_jumps(self, sample_sessions):
        with _mock_sessions(sample_sessions), \
             patch("claude_monitor.focus_terminal_session", return_value=True) as mock_jump:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.click("#session-table", offset=(2, 2), times=2)
                await settle(pilot)
                assert mock_jump.called
                assert len(pilot.app.screen_stack) == 1  # no menu

    async def test_double_click_on_highlighted_row_jumps(self, sample_sessions):
        """Regression: dbl-click on the already-selected row must not open
        the menu on the first click."""
        with _mock_sessions(sample_sessions), \
             patch("claude_monitor.focus_terminal_session", return_value=True) as mock_jump:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                # row 0 is highlighted on mount
                await pilot.click("#session-table", offset=(2, 1), times=2)
                await settle(pilot)
                assert mock_jump.called
                assert len(pilot.app.screen_stack) == 1

    async def test_menu_shows_options(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                assert options is not None
                assert options.option_count >= 5

    async def test_menu_escape_closes(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                assert len(pilot.app.screen_stack) > 1
                await pilot.press("escape")
                await settle(pilot)
                assert len(pilot.app.screen_stack) == 1

    async def test_menu_shows_session_title(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                screen = pilot.app.screen
                title_label = screen.query_one("#menu-title")
                label_text = str(title_label.render())
                # Menu title should match whichever session is highlighted
                session_titles = [s.title for s in sample_sessions]
                assert any(t in label_text for t in session_titles)

    async def test_menu_has_remote_link_when_available(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                option_ids = [options.get_option_at_index(i).id
                              for i in range(options.option_count)]
                assert "remote" in option_ids

    async def test_menu_no_remote_when_absent(self):
        sessions = [make_session(session_id="no-remote", remote_url="", slug="")]
        with _mock_sessions(sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                option_ids = [options.get_option_at_index(i).id
                              for i in range(options.option_count)]
                assert "remote" not in option_ids


class TestColumnPicker:
    async def test_column_picker_opens(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+c")
                await settle(pilot)
                assert len(pilot.app.screen_stack) > 1

    async def test_column_picker_escape_closes(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+c")
                await settle(pilot)
                assert len(pilot.app.screen_stack) > 1
                await pilot.press("escape")
                await settle(pilot)
                assert len(pilot.app.screen_stack) == 1

    async def test_column_toggle(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+c")
                await settle(pilot)
                screen = pilot.app.screen
                picker = screen
                ol = screen.query_one("#picker-list", OptionList)
                first_key = picker._col_keys[0]
                was_selected = first_key in picker.selected_cols
                await pilot.press("enter")
                await settle(pilot)
                assert (first_key in picker.selected_cols) != was_selected


class TestSearch:
    async def test_search_filters_sessions(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("slash")
                await settle(pilot)
                search = pilot.app.query_one("#search-bar", Input)
                search.value = "First"
                await settle(pilot)
                await settle(pilot)  # filtering runs via refresh_sessions' worker thread
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 1

    async def test_clear_search_restores_all(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("slash")
                await settle(pilot)
                search = pilot.app.query_one("#search-bar", Input)
                search.value = "First"
                await settle(pilot)
                await settle(pilot)  # filtering runs via refresh_sessions' worker thread
                await pilot.press("escape")
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count >= 3

    async def test_search_no_match(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("slash")
                await settle(pilot)
                search = pilot.app.query_one("#search-bar", Input)
                search.value = "nonexistent-session-xyz"
                await settle(pilot)
                await settle(pilot)  # filtering runs via refresh_sessions' worker thread
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 0

    async def test_letter_keys_type_into_search_not_hotkeys(self, sample_sessions):
        """While the search box has focus, plain letters are filter text,
        not the type-ahead group jump (every real hotkey needs Ctrl)."""
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                sort_before = pilot.app.sort_mode
                await pilot.press("slash")
                await settle(pilot)
                await pilot.press("s")
                await settle(pilot)
                search = pilot.app.query_one("#search-bar", Input)
                assert search.value == "s"
                assert pilot.app.sort_mode == sort_before

    async def test_down_from_search_drops_to_table_keeping_filter(
        self, sample_sessions
    ):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("slash")
                await settle(pilot)
                search = pilot.app.query_one("#search-bar", Input)
                search.value = "First"
                await settle(pilot)
                await settle(pilot)  # filtering runs via refresh_sessions' worker thread
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 1
                await pilot.press("down")
                await settle(pilot)
                assert table.has_focus
                assert pilot.app._filter == "First"
                assert table.row_count == 1
                assert search.display is True
                # Esc from the table still clears the filter and restores rows.
                await pilot.press("escape")
                await settle(pilot)
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
                await settle(pilot)
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
                await settle(pilot)
                s = pilot.app._flat_rows[0]
                pilot.app._make_menu_handler(s)("jump")
                clear.assert_not_called()

    async def test_i_toggles_detail_panel(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                panel = pilot.app.query_one("#detail-panel", Static)
                assert panel.display is True
                await pilot.press("ctrl+v")
                await settle(pilot)
                assert panel.display is False
                assert pilot.app.show_detail is False
                await pilot.press("ctrl+v")
                await settle(pilot)
                assert panel.display is True


class TestArchived:
    async def test_archive_toggle(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                assert pilot.app.show_archived is False
                await pilot.press("ctrl+z")
                await settle(pilot)
                assert pilot.app.show_archived is True
                await pilot.press("ctrl+z")
                await settle(pilot)
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
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                before = table.row_count
                await pilot.press("ctrl+z")
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count > before  # archived session appeared

    async def test_archived_menu_shows_resume(self):
        s = make_session(session_id="old-1", title="Old Session", status="archived")
        with _mock_sessions([s]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                option_ids = [options.get_option_at_index(i).id
                              for i in range(options.option_count)]
                assert "resume" in option_ids
                assert "jump" not in option_ids

    async def test_active_menu_shows_jump(self, sample_sessions):
        with _mock_sessions(sample_sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                screen = pilot.app.screen
                options = screen.query_one("#menu-options", OptionList)
                option_ids = [options.get_option_at_index(i).id
                              for i in range(options.option_count)]
                assert "jump" in option_ids
                assert "edit_name" in option_ids
                assert "resume" in option_ids


class TestHideInactivePins:
    """Fixture from 2026-08-16: pins must never expire on their own (Max:
    "pins should stay until I unpin them"). Ctrl+O is a separate on/off view
    filter, not an age-based decay, and it must never touch the pin itself,
    only whether a pinned-but-inactive row currently renders. "Inactive" is
    exactly the existing bold/dim test render_row() already uses (Max: "the
    same that makes a row not bold"): status in INACTIVE_STATUSES
    (archived, closed). That's a snapshot of current render state, not
    process liveness or usage history: a pin whose terminal happens to be
    closed at this instant (e.g. config-MCPs, resumed routinely rather than
    kept running) reads as inactive by this rule and IS hidden when the
    toggle is on, same as it renders dim right now — the accepted tradeoff
    of the simple rule Max asked for over a separate liveness check.
    "archived" (dim, same as closed) was the actual majority case the
    filter first shipped missing entirely, since it only checked for
    status == "closed"."""

    def _sessions(self):
        active = make_session(session_id="active-1", title="Active", status="working")
        pinned_closed = make_session(session_id="pinned-1", title="Pinned Closed",
                                     status="closed")
        return [active, pinned_closed]

    async def test_off_by_default_shows_closed_pin(self):
        sessions = self._sessions()
        with patch("claude_monitor.parse_sessions", return_value=sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                assert pilot.app.hide_inactive_pins is False
                pilot.app._pinned = {"pinned-1"}
                pilot.app.refresh_sessions()
                await settle(pilot)
                titles = [s.title for s in pilot.app._row_map if s]
                assert "Pinned Closed" in titles

    async def test_toggle_hides_and_restores_closed_pin(self):
        sessions = self._sessions()
        with patch("claude_monitor.parse_sessions", return_value=sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                pilot.app._pinned = {"pinned-1"}
                pilot.app.refresh_sessions()
                await settle(pilot)

                await pilot.press("ctrl+o")
                await settle(pilot)
                assert pilot.app.hide_inactive_pins is True
                titles = [s.title for s in pilot.app._row_map if s]
                assert "Pinned Closed" not in titles
                assert "Active" in titles  # never touches non-pinned rows

                await pilot.press("ctrl+o")
                await settle(pilot)
                assert pilot.app.hide_inactive_pins is False
                titles = [s.title for s in pilot.app._row_map if s]
                assert "Pinned Closed" in titles  # the pin itself never expired

    async def test_toggle_never_unpins(self):
        sessions = self._sessions()
        with patch("claude_monitor.parse_sessions", return_value=sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                pilot.app._pinned = {"pinned-1"}
                await pilot.press("ctrl+o")
                await settle(pilot)
                assert "pinned-1" in pilot.app._pinned

    async def test_hides_archived_pin_not_just_closed(self):
        """The bug actually reported: most real pins age past active_cutoff
        and get relabeled 'archived' (a separate, dimmer status than
        'closed', same rendering), and the filter's first version only ever
        checked for 'closed', so it left the vast majority of a real pin
        list untouched no matter the toggle."""
        archived_pinned = make_session(session_id="pinned-archived", title="Archived Pin",
                                       status="archived")
        with patch("claude_monitor.parse_sessions", return_value=[archived_pinned]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                pilot.app._pinned = {"pinned-archived"}
                pilot.app.refresh_sessions()
                await settle(pilot)
                titles = [s.title for s in pilot.app._row_map if s]
                assert "Archived Pin" in titles

                await pilot.press("ctrl+o")
                await settle(pilot)
                titles = [s.title for s in pilot.app._row_map if s]
                assert "Archived Pin" not in titles

    async def test_unpinned_archived_session_ignores_the_toggle(self):
        """The near-regression this filter almost shipped with: widening
        the toggle to catch "archived" pins by broadening the BASE "hide
        closed sessions" filter would also start excluding archived,
        UNPINNED sessions from the default view, which were never hidden
        by default at all, toggle on or off. That base filter must be a
        separate, untouched step from the hide_inactive_pins exclusion."""
        unpinned_archived = make_session(session_id="unpinned-archived", title="Old Unpinned",
                                         status="archived")
        with patch("claude_monitor.parse_sessions", return_value=[unpinned_archived]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                titles = [s.title for s in pilot.app._row_map if s]
                assert "Old Unpinned" in titles

                await pilot.press("ctrl+o")
                await settle(pilot)
                titles = [s.title for s in pilot.app._row_map if s]
                assert "Old Unpinned" in titles  # never pinned, toggle is irrelevant to it

    async def test_recently_done_pin_never_hidden(self):
        """A pin that just finished a turn ('done Xm ago') renders bold, the
        same as working, and must count as active no matter how long it's
        been since 'Xm ago' grows: this filter reads status, it never adds
        its own separate staleness clock."""
        done_pinned = make_session(session_id="pinned-done", title="Done Pin", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[done_pinned]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                pilot.app._pinned = {"pinned-done"}
                pilot.app.refresh_sessions()
                await settle(pilot)

                await pilot.press("ctrl+o")
                await settle(pilot)
                titles = [s.title for s in pilot.app._row_map if s]
                assert "Done Pin" in titles

    async def test_hiding_inactive_pin_does_not_demote_active_groupmate(self):
        """Fixture from 2026-08-16 (Max: "ctrl o is turning off some (only
        some) of the active pins"). A 2-member group loses its only inactive
        member to the hide filter; its still-visible active member must stay
        under its own group header, not fold into 'ungrouped' just because
        the hidden filter made the group LOOK like a singleton."""
        active = make_session(session_id="active-pin", title="strategy-a", status="working")
        closed_pinned = make_session(session_id="closed-pin", title="strategy-b",
                                     status="closed")
        with patch("claude_monitor.parse_sessions", return_value=[active, closed_pinned]):
            original = ClaudeMonitor.show_groups._default
            ClaudeMonitor.show_groups._default = True
            try:
                async with ClaudeMonitor().run_test(size=(200, 50)) as pilot:
                    await settle(pilot)
                    pilot.app._pinned = {"active-pin", "closed-pin"}
                    pilot.app.refresh_sessions()
                    await settle(pilot)
                    assert pilot.app._group_counts.get("strategy") == 2

                    await pilot.press("ctrl+o")
                    await settle(pilot)
                    assert pilot.app._group_counts.get("strategy") == 1
                    assert "ungrouped" not in pilot.app._group_counts
                    titles = [s.title for s in pilot.app._row_map if s]
                    assert "strategy-a" in titles
            finally:
                ClaudeMonitor.show_groups._default = original


class TestInboxMode:
    """Max, 2026-08-18: "an 'inbox mode' that changes it so that anything
    that is working state disappears from the monitor so that I can further
    hone my attention focus." Ctrl+B (Ctrl+I is Tab on the wire and cannot
    be bound; see the BINDINGS comment). Hides working and standby; keeps
    APPROVE and READY (seen or not). Persists like the other view toggles."""

    def _mix(self):
        return [
            make_session(session_id="w", title="alpha-working", status="working"),
            make_session(session_id="d", title="alpha-ready", status="done"),
            make_session(session_id="a", title="beta-approve", status="needs_approval"),
            make_session(session_id="s", title="config-skills", status="standby"),
        ]

    async def test_off_by_default_shows_everything(self):
        with _mock_sessions(self._mix()):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                assert pilot.app.inbox_mode is False
                titles = {s.title for s in pilot.app._row_map if s}
                assert titles == {"alpha-working", "alpha-ready", "beta-approve", "config-skills"}

    async def test_ctrl_b_hides_working_and_standby_keeps_needs_you(self):
        with _mock_sessions(self._mix()):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+b")
                await settle(pilot)
                assert pilot.app.inbox_mode is True
                titles = {s.title for s in pilot.app._row_map if s}
                assert titles == {"alpha-ready", "beta-approve"}

    async def test_seen_ready_stays_in_the_inbox(self):
        """Seen means de-emphasized, not dealt with: it is still yours to
        act on, so the inbox keeps it."""
        ready = make_session(session_id="d", title="ready", status="done", last_activity=100.0)
        with patch("claude_monitor.parse_sessions", return_value=[ready]), \
             patch("claude_monitor.load_prefs",
                   return_value={"acked_ready": {"d": [2, 100.0]}}), \
             patch("claude_monitor.save_prefs"):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+b")
                await settle(pilot)
                assert [s.title for s in pilot.app._row_map if s] == ["ready"]

    async def test_toggle_back_restores_everything(self):
        with _mock_sessions(self._mix()):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+b")
                await settle(pilot)
                await pilot.press("ctrl+b")
                await settle(pilot)
                assert pilot.app.inbox_mode is False
                assert len([s for s in pilot.app._row_map if s]) == 4

    async def test_hiding_working_members_does_not_demote_ready_groupmate(self):
        """Same demotion guard hide_inactive_pins needed on 2026-08-16: a
        group whose working members vanish must keep its one remaining
        READY row under its own header, not fold it into 'ungrouped'."""
        working = make_session(session_id="w", title="strategy-a", status="working")
        ready = make_session(session_id="d", title="strategy-b", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[working, ready]):
            original = ClaudeMonitor.show_groups._default
            ClaudeMonitor.show_groups._default = True
            try:
                async with ClaudeMonitor().run_test(size=(200, 50)) as pilot:
                    await settle(pilot)
                    assert pilot.app._group_counts.get("strategy") == 2
                    await pilot.press("ctrl+b")
                    await settle(pilot)
                    assert pilot.app._group_counts.get("strategy") == 1
                    assert "ungrouped" not in pilot.app._group_counts
                    assert [s.title for s in pilot.app._row_map if s] == ["strategy-b"]
            finally:
                ClaudeMonitor.show_groups._default = original

    async def test_cursor_on_a_hidden_row_lands_on_first_real_row_under_grouping(self):
        """Grouped view (the production default). Rows: [header, w1, w2, d1,
        d2]; cursor on w2; Ctrl+B. The refresh restores the cursor by sid,
        w2 is gone, and Textual leaves the cursor at row 0, which is the
        group HEADER (row_map[0] is None): Enter and n then do nothing. The
        restore path must fall back to the first real row. The earlier
        version of this test ran ungrouped with every survivor by
        construction non-working, so it could not fail (review,
        2026-08-18); this one asserts the exact row."""
        rows = [
            make_session(session_id="w1", title="strategy-a", status="working"),
            make_session(session_id="w2", title="strategy-b", status="working"),
            make_session(session_id="d1", title="strategy-c", status="done"),
            make_session(session_id="d2", title="strategy-d", status="done"),
        ]
        with patch("claude_monitor.parse_sessions", return_value=rows):
            original = ClaudeMonitor.show_groups._default
            ClaudeMonitor.show_groups._default = True
            try:
                async with ClaudeMonitor().run_test(size=(200, 50)) as pilot:
                    await settle(pilot)
                    table = pilot.app.query_one("#session-table", DataTable)
                    assert pilot.app._row_map[0] is None  # header first, as in prod
                    table.move_cursor(row=pilot.app._row_index_of("w2"))
                    await settle(pilot)
                    await pilot.press("ctrl+b")
                    await settle(pilot)
                    cur = pilot.app._row_map[table.cursor_row]
                    assert cur is not None, "cursor stranded on the group header"
                    assert cur.session_id == "d1"
            finally:
                ClaudeMonitor.show_groups._default = original

    async def test_persists_in_view_state(self):
        """_save_view_state is a no-op while PYTEST_CURRENT_TEST is set. An
        earlier version popped that env var around the whole app mount,
        which also un-gated on_mount's /dev/tty title write, the reconcile
        thread against the real ~/.claude/session-states, and the real
        port bind (review, 2026-08-18). Instead, call the save directly
        with the guard patched out for that one call."""
        with _mock_sessions(self._mix()), \
             patch("claude_monitor._update_prefs") as upd:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+b")
                await settle(pilot)
                assert pilot.app.inbox_mode is True
                upd.reset_mock()
                with patch.dict("os.environ", {}, clear=False):
                    import os
                    os.environ.pop("PYTEST_CURRENT_TEST", None)
                    pilot.app._save_view_state()
                saved = {}
                for call in upd.call_args_list:
                    call.args[0](saved)
                assert saved["view_state"]["inbox_mode"] is True

    async def test_restores_from_view_state(self):
        """_load_view_state is also a pytest no-op (it reads real prefs);
        call it directly with the guard bypassed and mocked prefs."""
        with _mock_sessions(self._mix()), \
             patch("claude_monitor.load_prefs",
                   return_value={"view_state": {"inbox_mode": True}}), \
             patch("claude_monitor.save_prefs"):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                assert pilot.app.inbox_mode is False
                with patch.dict("os.environ", {}, clear=False):
                    import os
                    os.environ.pop("PYTEST_CURRENT_TEST", None)
                    pilot.app._load_view_state()
                assert pilot.app.inbox_mode is True
                pilot.app.refresh_sessions()
                await settle(pilot)
                titles = {s.title for s in pilot.app._row_map if s}
                assert titles == {"alpha-ready", "beta-approve"}

    async def test_stats_counters_are_unfiltered_while_inbox_is_on(self):
        """Review 2026-08-18: with the filter on `sessions`, the bar read
        "0 working" right beside the INBOX chip, saying exactly what the
        chip exists to deny."""
        with _mock_sessions(self._mix()):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+b")
                await settle(pilot)
                working = pilot.app.query_one("#stats-working")
                assert "1 working" in str(working.render())

    async def test_bell_still_rings_for_a_session_hidden_while_working(self):
        """Review 2026-08-18: a session that spent its whole working life
        hidden never seeded _prev_statuses, so its APPROVE arrival rang no
        bell, the one event an inbox exists to surface."""
        w = make_session(session_id="x", title="x", status="working")
        with patch("claude_monitor.parse_sessions", return_value=[w]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+b")
                await settle(pilot)
                assert pilot.app._row_map == [] or all(r is None for r in pilot.app._row_map)
                w.status = "needs_approval"
                pilot.app.refresh_sessions()
                await settle(pilot)
                assert "x" in pilot.app._bell

    async def test_jump_by_name_finds_a_hidden_working_session(self, tmp_path, monkeypatch):
        """Review 2026-08-18: the request-file jump resolved against the
        filtered list, so `claude-jump <sid8>` for a working session
        reported "no session matching" while inbox was on."""
        import claude_monitor as cm
        monkeypatch.setattr(cm, "JUMP_REQUEST_PATH", tmp_path / "jump-request")
        w = make_session(session_id="abcd1234-0000", title="busy", status="working")
        with patch("claude_monitor.parse_sessions", return_value=[w]), \
             patch("claude_monitor.focus_terminal_session", return_value=True) as jump:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+b")
                await settle(pilot)
                cm.JUMP_REQUEST_PATH.write_text("abcd1234")
                pilot.app._check_jump_request()
                await settle(pilot)
                jump.assert_called_once()

    async def test_stats_bar_shows_inbox_chip_only_when_on(self):
        with _mock_sessions(self._mix()):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                chip = pilot.app.query_one("#stats-inbox")
                assert "INBOX" not in str(chip.render())
                await pilot.press("ctrl+b")
                await settle(pilot)
                rendered = str(chip.render())
                assert "INBOX" in rendered
                assert "2 hidden" in rendered  # alpha-working + config-skills


class TestCursorRowKeepsStatusColors:
    """Max, 2026-08-18: "the blue is still only showing when I select a
    window other than monitor." Measured: Textual's default FOCUSED
    DataTable cursor paints a solid $primary band and forces the row's
    foreground to white, so the READY colour vanished on exactly the row
    he had selected and came back the moment the window blurred (band
    goes translucent, override goes away). The cursor must never override
    the row's foreground, in either focus state, in either theme."""

    async def test_focused_cursor_does_not_force_white_foreground(self):
        rows = [make_session(session_id="a", title="ready", status="done")]
        for dark in (False, True):
            with patch("claude_monitor.parse_sessions", return_value=rows), \
                 patch("claude_monitor._system_is_dark", return_value=dark):
                app = ClaudeMonitor()
                async with app.run_test() as pilot:
                    await settle(pilot)
                    await settle(pilot)
                    table = app.query_one("#session-table", DataTable)
                    table.focus()
                    await settle(pilot)
                    focused = table.get_component_styles("datatable--cursor")
                    assert focused.color.rgb != (255, 255, 255), app.theme
                    app.set_focus(None)
                    await settle(pilot)
                    blurred = table.get_component_styles("datatable--cursor")
                    # Same text colour whether or not the table has focus.
                    assert focused.color.rgb == blurred.color.rgb, app.theme
                    # The band itself must still be visible (not fully transparent).
                    assert focused.background.a > 0.2, app.theme


class TestSubagents:
    async def test_subagents_shown_when_toggled(self):
        sub = make_session(session_id="sub-1", title="agent-1", is_subagent=True,
                           parent_id="parent-1")
        parent = make_session(session_id="parent-1", title="Parent", subagents=[sub])
        with _mock_sessions([parent]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 1
                await pilot.press("ctrl+a")
                await settle(pilot)
                assert table.row_count == 2

    async def test_subagents_hidden_again(self):
        sub = make_session(session_id="sub-1", is_subagent=True, parent_id="p1")
        parent = make_session(session_id="p1", subagents=[sub])
        with _mock_sessions([parent]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("ctrl+a")
                await settle(pilot)
                await pilot.press("ctrl+a")
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                assert table.row_count == 1


class TestEmptyState:
    async def test_mount_with_no_sessions_does_not_crash(self):
        # Regression: cursor_row is -1 on an empty DataTable; must not index
        # into _row_map / old_map with that.
        with patch("claude_monitor.parse_sessions", return_value=[]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                # Explicitly drive a refresh cycle too
                pilot.app.refresh_sessions()
                await settle(pilot)
                await settle(pilot)  # refresh_sessions runs in a worker thread
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
                await settle(pilot)
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
                await settle(pilot)
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
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                for i, s in enumerate(pilot.app._row_map):
                    if s and s.title.startswith("bashing-"):
                        table.move_cursor(row=i)
                        break
                await pilot.press("P")
                await settle(pilot)
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
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                for i, s in enumerate(pilot.app._row_map):
                    if s and s.session_id == "other-1":
                        table.move_cursor(row=i)
                        break
                await pilot.press("P")
                await settle(pilot)
                mock_bc.assert_not_called()


class TestTypeaheadJump:
    """A plain letter, typed in sequence like Finder/Explorer type-ahead
    find, jumps the cursor to the group whose name matches the accumulated
    buffer (e.g. s t r a -> 'strategy'). Every real hotkey now requires
    Ctrl, so a bare letter is always free for this."""

    @pytest.fixture
    def grouped_sessions(self):
        # A singleton group folds into "ungrouped" (see TestProactiveGroup);
        # strategy needs 2+ members to render as its own group header.
        return [
            make_session(session_id="bash-1", title="bashing-alpha", status="idle"),
            make_session(session_id="bash-2", title="bashing-beta", status="working"),
            make_session(session_id="strat-1", title="strategy-main", status="idle"),
            make_session(session_id="strat-2", title="strategy-teamplan", status="idle"),
            make_session(session_id="other-1", title="other-thing", status="idle"),
        ]

    async def test_single_letter_jumps_to_group_header(self, grouped_sessions):
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                await pilot.press("s")
                await settle(pilot)
                cr = table.cursor_row
                assert pilot.app._row_map[cr] is None  # landed on a header row
                nxt = next(s for s in pilot.app._row_map[cr:] if s)
                assert nxt.title.startswith("strategy")

    async def test_multi_letter_sequence_narrows_match(self, grouped_sessions):
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                for k in ("b", "a", "s", "h"):
                    await pilot.press(k)
                await settle(pilot)
                cr = table.cursor_row
                nxt = next(s for s in pilot.app._row_map[cr:] if s)
                assert nxt.title.startswith("bashing")
                assert pilot.app._typeahead_buffer == "bash"

    async def test_timeout_resets_buffer(self, grouped_sessions):
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("b")
                await settle(pilot)
                pilot.app._typeahead_last_key -= 10  # force timeout
                await pilot.press("s")
                await settle(pilot)
                # Stale "b" buffer must not survive; fresh "s" alone matches strategy.
                assert pilot.app._typeahead_buffer == "s"
                table = pilot.app.query_one("#session-table", DataTable)
                nxt = next(s for s in pilot.app._row_map[table.cursor_row:] if s)
                assert nxt.title.startswith("strategy")

    async def test_no_match_does_not_move_cursor(self, grouped_sessions):
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                table.move_cursor(row=0)
                await pilot.press("z")
                await settle(pilot)
                assert table.cursor_row == 0

    async def test_ignored_while_modal_open(self, grouped_sessions):
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                row = next(i for i, s in enumerate(pilot.app._row_map) if s)
                table.move_cursor(row=row)
                await pilot.press("enter")  # opens SessionMenu (a real row, not a header)
                await settle(pilot)
                assert len(pilot.app.screen_stack) > 1
                await pilot.press("s")
                await settle(pilot)
                await pilot.press("escape")
                await settle(pilot)
                assert table.cursor_row == row

    async def test_ignored_while_search_focused(self, grouped_sessions):
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                table.move_cursor(row=0)
                await pilot.press("slash")
                await settle(pilot)
                await pilot.press("s")
                await settle(pilot)
                assert table.cursor_row == 0

    async def test_ungrouped_view_jumps_to_first_row_of_group(self, grouped_sessions):
        with patch("claude_monitor.parse_sessions", return_value=grouped_sessions), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                pilot.app.show_groups = False
                pilot.app.refresh_sessions()
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                await pilot.press("s")
                await settle(pilot)
                sel = pilot.app._row_map[table.cursor_row]
                assert sel is not None and sel.title.startswith("strategy")


class TestTitleDisambiguation:
    async def test_duplicate_titles_get_sid_suffix(self):
        dups = [
            make_session(session_id="abc12345-x", title="Shared Name", status="idle"),
            make_session(session_id="def67890-y", title="Shared Name", status="working"),
            make_session(session_id="zzz99999-z", title="Unique", status="idle"),
        ]
        with _mock_sessions(dups):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
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
                    await settle(pilot)
                    pilot._saved = saved  # type: ignore
                    yield pilot
        return _ctx()

    async def test_delete_works_outside_history_mode(self, archived_sessions):
        with _mock_sessions(archived_sessions), \
             patch("claude_monitor.load_hidden_sessions", return_value=set()), \
             patch("claude_monitor.save_hidden_sessions"):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                # show_archived defaults False; cursor on archived row 0
                await pilot.press("backspace")
                await settle(pilot)
                assert pilot.app._delete_armed_for is not None

    async def test_delete_arms_then_hides(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)  # arch-1
            await settle(pilot)
            await pilot.press("backspace")
            await settle(pilot)
            assert pilot.app._delete_armed_for == frozenset({"arch-1"})
            assert "arch-1" not in pilot.app._hidden
            await pilot.press("backspace")
            await settle(pilot)
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
            await settle(pilot)
            await pilot.press("backspace")
            await settle(pilot)
            assert pilot.app._delete_armed_for is None
            assert "live-1" not in pilot.app._hidden

    async def test_shift_down_extends_selection(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await settle(pilot)
            await pilot.press("shift+down")
            await settle(pilot)
            assert len(pilot.app._selection) == 2
            await pilot.press("shift+down")
            await settle(pilot)
            assert len(pilot.app._selection) == 3

    async def test_plain_arrow_clears_selection(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await settle(pilot)
            await pilot.press("shift+down")
            await settle(pilot)
            assert pilot.app._selection
            await pilot.press("down")
            await settle(pilot)
            assert pilot.app._selection == set()

    async def test_batch_hide_via_selection(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await settle(pilot)
            await pilot.press("shift+down", "shift+down")  # select 3 rows
            await settle(pilot)
            await pilot.press("backspace")  # arm
            await settle(pilot)
            armed = pilot.app._delete_armed_for
            assert armed is not None and len(armed) >= 2
            await pilot.press("backspace")  # confirm
            await settle(pilot)
            assert pilot.app._hidden >= {"arch-1", "arch-2"}

    async def test_shift_up_shrinks_selection(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await settle(pilot)
            await pilot.press("shift+down", "shift+down")
            await settle(pilot)
            n_before = len(pilot.app._selection)
            await pilot.press("shift+up")
            await settle(pilot)
            assert len(pilot.app._selection) == n_before - 1
            assert pilot.app._selection_anchor is not None

    async def test_selection_survives_refresh(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            pilot.app._extending_cursor = True
            table.move_cursor(row=1)  # anchor NOT at row 0
            pilot.app._extending_cursor = False
            await settle(pilot)
            await pilot.press("shift+down")
            await settle(pilot)
            sel_before = set(pilot.app._selection)
            anchor_before = pilot.app._selection_anchor
            pilot.app.refresh_sessions()
            await settle(pilot)
            assert pilot.app._selection == sel_before
            assert pilot.app._selection_anchor == anchor_before

    async def test_hide_preserves_cursor_at_survivor(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            # cursor on row 1 (arch-2)
            pilot.app._extending_cursor = True
            table.move_cursor(row=1)
            pilot.app._extending_cursor = False
            await settle(pilot)
            await pilot.press("backspace", "backspace")
            await settle(pilot)
            assert "arch-2" in pilot.app._hidden
            # arch-2 gone; cursor should land on next survivor (arch-3), not row 0
            cur = pilot.app._row_map[table.cursor_row]
            assert cur is not None
            assert cur.session_id != "arch-1"  # not reset to top

    async def test_cursor_move_disarms_delete(self, archived_sessions):
        async with await self._app(archived_sessions) as pilot:
            table = pilot.app.query_one("#session-table", DataTable)
            table.move_cursor(row=0)
            await settle(pilot)
            await pilot.press("backspace")
            await settle(pilot)
            assert pilot.app._delete_armed_for is not None
            await pilot.press("down")
            await settle(pilot)
            assert pilot.app._delete_armed_for is None


class TestNextActionableMovesCursorOnly:
    """Fixture from 2026-08-16, corrected same day: "n" was first built to
    jump immediately, which Max caught and reversed: "n in monitor should
    just move the highlighted row [...] n-Enter-Enter would also jump but
    not just naked n." Ctrl+Shift+N (claude-monitor --jump-next) is the
    actual jump command, usable from inside the monitor or anywhere else on
    the machine; plain "n" only ever repositions the cursor."""

    def _sessions(self):
        return [
            make_session(session_id="working-1", title="Working", status="working"),
            make_session(session_id="ready-1", title="Ready One", status="done",
                        last_activity=100),
            make_session(session_id="approve-1", title="Needs Approval",
                        status="needs_approval", last_activity=200),
        ]

    async def test_n_moves_cursor_without_jumping(self):
        sessions = self._sessions()
        with patch("claude_monitor.parse_sessions", return_value=sessions), \
             patch("claude_monitor.focus_terminal_session") as jump, \
             patch("claude_monitor.resume_session") as resume:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("n")
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                cursor_session = pilot.app._row_map[table.cursor_row]
                assert cursor_session.session_id == "approve-1"
                jump.assert_not_called()
                resume.assert_not_called()

    async def test_n_does_not_touch_shared_next_cursor_or_read_mark(self):
        """Browsing with n is not the same as visiting: it must not advance
        the queue position Ctrl+Shift+N walks, or mark a READY row seen."""
        sessions = self._sessions()
        with patch("claude_monitor.parse_sessions", return_value=sessions), \
             patch("claude_monitor.save_prefs") as save, \
             patch("claude_monitor.focus_terminal_session"):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                save.reset_mock()  # drop the startup launch_count save
                await pilot.press("n")
                await settle(pilot)
                for call in save.call_args_list:
                    prefs_arg = call[0][0]
                    assert "next_cursor" not in prefs_arg
                    assert "acked_ready" not in prefs_arg

    async def test_n_with_nothing_actionable_notifies(self):
        sessions = [make_session(session_id="w", title="Working", status="working")]
        with patch("claude_monitor.parse_sessions", return_value=sessions), \
             patch("claude_monitor.focus_terminal_session") as jump:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("n")
                await settle(pilot)
                jump.assert_not_called()

    async def test_n_cycles_from_current_cursor_not_the_start(self):
        a = make_session(session_id="a", title="A", status="done", last_activity=100)
        b = make_session(session_id="b", title="B", status="done", last_activity=200)
        with patch("claude_monitor.parse_sessions", return_value=[a, b]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)

                await pilot.press("n")
                await settle(pilot)
                assert pilot.app._row_map[table.cursor_row].session_id == "a"

                await pilot.press("n")
                await settle(pilot)
                assert pilot.app._row_map[table.cursor_row].session_id == "b"

                await pilot.press("n")  # wraps back around
                await settle(pilot)
                assert pilot.app._row_map[table.cursor_row].session_id == "a"

    async def test_n_walks_table_order_not_priority_order(self):
        """Bug reported by Max (2026-08-17): "naked n just skipped a READY
        claude row." Cause: n used to reuse find_next_actionable(), which
        cycles by urgency (oldest-waiting-first) rather than by what's
        visually next in the table, so it could leap over a row you can
        see is READY to reach an older one further down. Table order here
        (alphabetical, matching what's on screen) is A, B, C, D; last_
        activity order (what the old code cycled by) is B, D, C, A (B
        oldest). Starting from B: table order says next is C; the old
        priority order would have said D, skipping over the visible,
        actionable C entirely."""
        a = make_session(session_id="a", title="A", status="done", last_activity=400)
        b = make_session(session_id="b", title="B", status="done", last_activity=100)
        c = make_session(session_id="c", title="C", status="done", last_activity=300)
        d = make_session(session_id="d", title="D", status="done", last_activity=200)
        with patch("claude_monitor.parse_sessions", return_value=[a, b, c, d]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                b_row = pilot.app._row_index_of("b")
                table.move_cursor(row=b_row)

                await pilot.press("n")
                await settle(pilot)
                assert pilot.app._row_map[table.cursor_row].session_id == "c"

    async def test_n_enter_enter_still_jumps(self):
        """n moves the cursor; Enter then opens the menu on that row, and a
        second Enter picks its default (Jump for a live session), the
        combination Max named as the intended way to actually jump via n."""
        sessions = self._sessions()
        with patch("claude_monitor.parse_sessions", return_value=sessions), \
             patch("claude_monitor.focus_terminal_session", return_value=True) as jump:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("n")
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                jump.assert_called_once()
                assert jump.call_args[0][0].session_id == "approve-1"

    async def test_n_only_considers_search_filtered_visible_rows(self):
        """Bug caught by review (2026-08-16): action_jump_next_actionable()
        used to pick from self.sessions (unfiltered by the active search
        box) but move the cursor via self._row_map (filtered), so a target
        hidden by search silently no-opped: find_next_actionable found it,
        but _row_index_of couldn't locate it in the visible table at all.
        Must pick from self._flat_rows, the same filtered set the table
        itself renders from."""
        sessions = self._sessions()  # ready-1 and approve-1 are the actionable ones
        with patch("claude_monitor.parse_sessions", return_value=sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                pilot.app._filter = "working"  # matches only working-1 (not actionable)
                pilot.app.refresh_sessions()
                await settle(pilot)
                table = pilot.app.query_one("#session-table", DataTable)
                before = table.cursor_row
                await pilot.press("n")
                await settle(pilot)
                # ready-1 and approve-1 are both hidden by the filter: nothing to move to.
                assert table.cursor_row == before


class TestReadyOnlyMarkedSeenOnActualJump:
    """Bug caught by review (2026-08-16): _mark_ready_seen() ran before
    checking whether the jump actually landed anywhere, so a failed jump
    (window not found, not alive enough to resume) still persisted the
    session as seen. CLAUDE.md's stated invariant is "only an actual jump
    marks a session seen" — these confirm the fix holds at every call site
    that jumps: the SessionMenu action and the headless --jump-next path."""

    def test_headless_path_does_not_mark_seen_when_window_not_found(self):
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]), \
             patch("claude_monitor.load_pinned_sessions", return_value=set()), \
             patch("claude_monitor.load_prefs", return_value={}), \
             patch("claude_monitor.save_prefs"), \
             patch("claude_monitor.focus_terminal_session", return_value=False), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor._heal_hook_state"), \
             patch("claude_monitor._mark_ready_seen") as mark_seen:
            from claude_monitor import jump_to_next_actionable
            ok, _msg, _s = jump_to_next_actionable()
            assert ok is False
            mark_seen.assert_not_called()

    def test_headless_path_marks_seen_on_successful_jump(self):
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]), \
             patch("claude_monitor.load_pinned_sessions", return_value=set()), \
             patch("claude_monitor.load_prefs", return_value={}), \
             patch("claude_monitor.save_prefs"), \
             patch("claude_monitor.focus_terminal_session", return_value=True), \
             patch("claude_monitor._mark_ready_seen") as mark_seen:
            from claude_monitor import jump_to_next_actionable
            ok, _msg, _s = jump_to_next_actionable()
            assert ok is True
            mark_seen.assert_called_once_with("ready-1", "done", target.last_activity)

    async def test_session_menu_jump_does_not_mark_seen_when_window_not_found(self):
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]), \
             patch("claude_monitor.focus_terminal_session", return_value=False), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor._heal_hook_state"), \
             patch("claude_monitor._mark_ready_seen") as mark_seen:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                handler = pilot.app._make_menu_handler(target)
                handler("jump")
                await settle(pilot)
                mark_seen.assert_not_called()

    async def test_session_menu_jump_marks_seen_on_success(self):
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]), \
             patch("claude_monitor.focus_terminal_session", return_value=True), \
             patch("claude_monitor._mark_ready_seen") as mark_seen:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                handler = pilot.app._make_menu_handler(target)
                handler("jump")
                await settle(pilot)
                mark_seen.assert_called_once_with("ready-1", "done", target.last_activity)

    async def test_refresh_never_writes_acked_ready(self):
        """Bug reported by Max (2026-08-16): a session he'd just marked seen
        turned back to yellow on its own. Cause: the refresh cycle used to
        read acked_ready, prune it to currently-done sessions, and write
        the pruned copy back every ~3s; a jump landing mid-cycle wrote its
        new seen mark, and the next refresh cycle (holding a prefs snapshot
        read BEFORE that jump) clobbered it back out on its own save. Fixed
        by making the refresh path read-only for this field: only
        _mark_ready_seen() writes acked_ready now. This drives several full
        refresh cycles with a session already marked seen (the exact
        precondition the old code would have pruned-and-written back) and
        asserts no save_prefs call ever narrows or drops that value (an
        unrelated save, e.g. launch_count, legitimately carries the same
        dict's other keys along, so the check is on the value, not on
        whether the key appears at all)."""
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]), \
             patch("claude_monitor.load_prefs", return_value={"acked_ready": ["ready-1"]}), \
             patch("claude_monitor.save_prefs") as save:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                for _ in range(3):
                    pilot.app.refresh_sessions()
                    await settle(pilot)
                for call in save.call_args_list:
                    saved = call[0][0]
                    if "acked_ready" in saved:
                        assert saved["acked_ready"] == ["ready-1"]


class TestJumpRequestOwnership:
    """Advisor review 2026-08-18: two monitor instances both polled the
    shared request file (only the HTTP bind was exclusive), so whichever
    ticked first served Ctrl+Shift+N against ITS filtered session list and
    advanced the shared next_cursor. Only the instance that owns the port
    may consume requests; a port collision demotes the other."""

    async def test_default_owner_consumes_requests(self, tmp_path, monkeypatch):
        import claude_monitor as cm
        monkeypatch.setattr(cm, "JUMP_REQUEST_PATH", tmp_path / "jump-request")
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                assert pilot.app._owns_jump_requests is True
                with patch.object(pilot.app, "_handle_jump_next_request") as handler:
                    cm.JUMP_REQUEST_PATH.write_text("__jump_next__:1:1")
                    pilot.app._check_jump_request()
                    handler.assert_called_once()

    async def test_demoted_instance_leaves_the_request_alone(self, tmp_path, monkeypatch):
        import claude_monitor as cm
        monkeypatch.setattr(cm, "JUMP_REQUEST_PATH", tmp_path / "jump-request")
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                pilot.app._owns_jump_requests = False  # what a port collision sets
                with patch.object(pilot.app, "_handle_jump_next_request") as handler:
                    cm.JUMP_REQUEST_PATH.write_text("__jump_next__:1:1")
                    pilot.app._check_jump_request()
                    handler.assert_not_called()
                    assert cm.JUMP_REQUEST_PATH.exists()  # left for the owner


class TestJumpRequestSentinelDispatch:
    """--jump-next/--restart append a unique ":<pid>:<ns>" token to their
    sentinel so a caller can tell its own request apart from an
    overlapping one (see _drop_request_and_await_consumption). The
    monitor's own request-file poller has to match on startswith, not ==,
    to still recognize a token-suffixed sentinel as the same command.

    Every test here monkeypatches the module-level JUMP_REQUEST_PATH to a
    scratch path (tmp_path), not the real /tmp/claude-jump-request: that
    file is shared with any monitor actually running on this machine, and
    writing a real sentinel to it from a test would fire an unintended
    restart or jump on it. _check_jump_request()/_start_jump_server()
    reference JUMP_REQUEST_PATH directly rather than caching it into a
    class attribute at class-definition time, precisely so a monkeypatch
    like this one takes effect (review, 2026-08-17)."""

    async def test_jump_next_sentinel_with_token_dispatches_to_fast_path(self, tmp_path, monkeypatch):
        import claude_monitor as cm
        monkeypatch.setattr(cm, "JUMP_REQUEST_PATH", tmp_path / "jump-request")
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                with patch.object(pilot.app, "_handle_jump_next_request") as handler:
                    cm.JUMP_REQUEST_PATH.write_text("__jump_next__:12345:999")
                    pilot.app._check_jump_request()
                    handler.assert_called_once()

    async def test_restart_sentinel_with_token_dispatches_to_restart(self, tmp_path, monkeypatch):
        import claude_monitor as cm
        monkeypatch.setattr(cm, "JUMP_REQUEST_PATH", tmp_path / "jump-request")
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                with patch.object(pilot.app, "action_restart") as restart:
                    cm.JUMP_REQUEST_PATH.write_text("__restart__:12345:999")
                    pilot.app._check_jump_request()
                    restart.assert_called_once()

    async def test_unrelated_sid_request_is_not_treated_as_a_sentinel(self, tmp_path, monkeypatch):
        """A real sid/title request must still take the generic match path,
        not get accidentally swallowed by a startswith() sentinel check."""
        import claude_monitor as cm
        monkeypatch.setattr(cm, "JUMP_REQUEST_PATH", tmp_path / "jump-request")
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]), \
             patch("claude_monitor.focus_terminal_session", return_value=True) as jump:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                cm.JUMP_REQUEST_PATH.write_text("ready-1")
                pilot.app._check_jump_request()
                # The jump runs on a worker thread; one pause() is not
                # always enough under load (see CLAUDE.md test notes).
                await settle(pilot)
                jump.assert_called_once()


class TestJumpNextRequestFailureFeedback:
    """Bug caught by review (2026-08-17): _handle_jump_next_request's
    background worker discarded _focus_or_resume_target()'s return value
    entirely, so a failed Ctrl+Shift+N jump (window not found, can't
    resume) left the user with no feedback at all, not even a toast."""

    async def test_notifies_on_failed_jump(self):
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]), \
             patch("claude_monitor.load_prefs", return_value={}), \
             patch("claude_monitor.save_prefs"), \
             patch("claude_monitor.focus_terminal_session", return_value=False), \
             patch("claude_monitor._is_session_alive", return_value=False), \
             patch("claude_monitor.resume_session", return_value=False):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                with patch.object(pilot.app, "notify") as notify:
                    pilot.app._handle_jump_next_request()
                    await settle(pilot)
                    notify.assert_called_once()
                    assert "Ready" in notify.call_args[0][0]

    async def test_does_not_notify_on_success(self):
        target = make_session(session_id="ready-1", title="Ready", status="done")
        with patch("claude_monitor.parse_sessions", return_value=[target]), \
             patch("claude_monitor.load_prefs", return_value={}), \
             patch("claude_monitor.save_prefs"), \
             patch("claude_monitor._mark_ready_seen"), \
             patch("claude_monitor.focus_terminal_session", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                with patch.object(pilot.app, "notify") as notify:
                    pilot.app._handle_jump_next_request()
                    await settle(pilot)
                    notify.assert_not_called()


class TestJumpNextSkipsAlreadySeen:
    """Bug reported by Max (2026-08-17): "I don't want ctrl-shift-n to jump
    me to an already read, redundantly... it keeps jumping me to X and I
    don't need it, I already jumped and took no action" / "since the
    unread isn't working the READY state is sticking and I am wasting
    time checking things I already decided to deal with later." The fast
    path (Ctrl+Shift+N with a monitor running) must read acked_ready from
    prefs and exclude those sessions from its candidates."""

    async def test_skips_seen_session_lands_on_unseen_one(self):
        seen = make_session(session_id="seen", title="Seen", status="done", last_activity=100)
        unseen = make_session(session_id="unseen", title="Unseen", status="done", last_activity=50)
        with patch("claude_monitor.parse_sessions", return_value=[seen, unseen]), \
             patch("claude_monitor.load_prefs",
                   return_value={"acked_ready": {"seen": [1, 100.0]}}), \
             patch("claude_monitor.save_prefs"), \
             patch("claude_monitor.focus_terminal_session", return_value=True) as jump:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                pilot.app._handle_jump_next_request()
                await settle(pilot)
                jump.assert_called_once()
                assert jump.call_args[0][0].session_id == "unseen"

    async def test_all_seen_notifies_nothing_needs_you_without_jumping(self):
        seen = make_session(session_id="seen", title="Seen", status="done", last_activity=100.0)
        with patch("claude_monitor.parse_sessions", return_value=[seen]), \
             patch("claude_monitor.load_prefs",
                   return_value={"acked_ready": {"seen": [1, 100.0]}}), \
             patch("claude_monitor.save_prefs"), \
             patch("claude_monitor.focus_terminal_session") as jump:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                with patch.object(pilot.app, "notify") as notify:
                    pilot.app._handle_jump_next_request()
                    await settle(pilot)
                    jump.assert_not_called()
                    notify.assert_called_once_with("Nothing needs you right now", timeout=3)


class TestBell:
    async def test_no_bell_on_startup(self):
        sessions = [make_session(session_id="s1", title="t", status="needs_approval")]
        with _mock_sessions(sessions):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                assert pilot.app._bell == {}

    async def test_rings_on_working_to_needs_approval(self):
        s = make_session(session_id="s1", title="t", status="working")
        with patch("claude_monitor.parse_sessions", side_effect=[[s], [s], [s]]):
            original = ClaudeMonitor.show_groups._default
            ClaudeMonitor.show_groups._default = False
            try:
                async with ClaudeMonitor().run_test() as pilot:
                    await settle(pilot)
                    assert pilot.app._bell == {}
                    s.status = "needs_approval"
                    pilot.app.refresh_sessions()
                    await settle(pilot)
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
                    await settle(pilot)
                    s.status = "needs_approval"
                    pilot.app.refresh_sessions()
                    await settle(pilot)
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
                    await settle(pilot)
                    s.status = "needs_approval"
                    pilot.app.refresh_sessions()
                    await settle(pilot)
                    assert "s1" in pilot.app._bell
                    s.status = "working"
                    pilot.app.refresh_sessions()
                    await settle(pilot)
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
                await settle(pilot)
                with patch.object(pilot.app, "_save_view_state"), \
                     patch("claude_monitor.subprocess.run",
                           side_effect=OSError(2, "No such file or directory")):
                    pilot.app.action_restart()  # must not raise
                    await settle(pilot)
                assert pilot.app.return_code == RESTART_EXIT_CODE


class TestSendPromptWithSpace:
    """Max, 2026-08-28: "make the spacebar allow us to send a prompt to a
    claude, similar to how it works in claude code pressing left arrow."
    Space opens a box on the cursor's session; Enter sends the text to
    that session's terminal and leaves you in the monitor."""

    def _live(self, **kw):
        return make_session(session_id="live-1", title="alpha", status="done", **kw)

    async def test_space_opens_the_prompt_box_named_for_the_target(self):
        s = self._live()
        with _mock_sessions([s]), \
             patch("claude_monitor._is_session_alive", return_value=True):
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("space")
                await settle(pilot)
                from claude_monitor import PromptSend
                screen = pilot.app.screen
                assert isinstance(screen, PromptSend)
                assert screen._target == "alpha"      # box names its target
                assert screen.query_one("#prompt-input", Input).has_focus

    async def test_enter_sends_the_typed_text_to_that_session(self):
        s = self._live()
        with _mock_sessions([s]), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor._send_to_terminal_session", return_value=True) as send:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("space")
                await settle(pilot)
                pilot.app.screen.query_one("#prompt-input", Input).value = "ship it"
                await pilot.press("enter")
                await settle(pilot)
                send.assert_called_once()
                sent_session, sent_text = send.call_args[0][0], send.call_args[0][1]
                assert sent_session.session_id == "live-1"
                assert sent_text == "ship it"

    async def test_it_returns_to_the_monitor_rather_than_leaving_you_there(self):
        s = self._live()
        with _mock_sessions([s]), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor._send_to_terminal_session", return_value=True) as send:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("space")
                await settle(pilot)
                pilot.app.screen.query_one("#prompt-input", Input).value = "hi"
                await pilot.press("enter")
                await settle(pilot)
                assert send.call_args.kwargs.get("return_to_monitor") is True

    async def test_escape_sends_nothing(self):
        s = self._live()
        with _mock_sessions([s]), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor._send_to_terminal_session") as send:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("space")
                await settle(pilot)
                await pilot.press("escape")
                await settle(pilot)
                send.assert_not_called()

    async def test_empty_prompt_sends_nothing(self):
        s = self._live()
        with _mock_sessions([s]), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor._send_to_terminal_session") as send:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("space")
                await settle(pilot)
                await pilot.press("enter")
                await settle(pilot)
                send.assert_not_called()

    async def test_a_session_that_is_not_running_refuses_before_opening(self):
        """Typing at a dead terminal lands the text nowhere, or worse, in
        whatever now owns that window."""
        s = make_session(session_id="gone-1", title="closed one", status="closed")
        with _mock_sessions([s]), \
             patch("claude_monitor._is_session_alive", return_value=False), \
             patch("claude_monitor._send_to_terminal_session") as send:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                await pilot.press("space")
                await settle(pilot)
                from claude_monitor import PromptSend
                assert not isinstance(pilot.app.screen, PromptSend)
                send.assert_not_called()

    async def test_space_still_does_nothing_on_a_group_header_row(self):
        with _mock_sessions([self._live()]), \
             patch("claude_monitor._is_session_alive", return_value=True), \
             patch("claude_monitor._send_to_terminal_session") as send:
            async with ClaudeMonitor().run_test() as pilot:
                await settle(pilot)
                with patch.object(pilot.app, "_cursor_session", return_value=None):
                    await pilot.press("space")
                    await settle(pilot)
                send.assert_not_called()
