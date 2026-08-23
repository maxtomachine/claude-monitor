"""The needs-you lifecycle, end to end through the real app.

Every real bug this week lived BETWEEN the units: acks that never expired
(#51), a refresh write-back that clobbered a fresh ack, seen-marks that
made Ctrl+Shift+N skip a session that had since done new work, inbox mode
starving the bell. Each unit passed. This file drives one ClaudeMonitor
through a session's whole life with REAL prefs persistence (the conftest
fixture points PREFS_PATH at a per-test scratch file, so load_prefs /
save_prefs / _mark_ready_seen / _update_prefs all run for real) and the
real Ctrl+Shift+N fast-path handler, mocking only parse_sessions (the
filesystem walk) per step. If a future change breaks the contract at any
step, the failing assertion names the step.

Timeline for session A (sid "a"):
  t1 working -> t2 done (READY, unseen, bell-free)
  jump via Ctrl+Shift+N -> seen once (mint, bold badge, title unbold)
  jump again, no action -> seen twice (badge unbolds)
  t3 A does new work: working -> t4 done again
      => ack VOID: unseen again, and Ctrl+Shift+N lands on it again
  t5 A asks for approval -> bell rings (even with inbox mode on)
  Shift+R-style clear -> every ack gone
"""

from unittest.mock import patch

from textual.widgets import DataTable

import claude_monitor as cm
from claude_monitor import ClaudeMonitor, READY_COLOR_DARK, READY_SEEN_COLOR
from tests.helpers import make_session, settle


def _status_cell(app, sid: str) -> str:
    """The Rich markup the table holds for this session's status cell."""
    return app._last_rendered[sid][1]


def _title_cell(app, sid: str) -> str:
    return app._last_rendered[sid][0]


class TestNeedsYouLifecycle:
    async def test_full_lifecycle(self):
        a = make_session(session_id="a", title="alpha", status="working", last_activity=1000.0)
        b = make_session(session_id="b", title="beta", status="working", last_activity=1000.0)
        live = [a, b]

        jumps: list[str] = []

        def fake_focus(session):
            jumps.append(session.session_id)
            return True

        with patch("claude_monitor.parse_sessions", side_effect=lambda **kw: list(live)), \
             patch("claude_monitor._system_is_dark", return_value=True), \
             patch("claude_monitor.focus_terminal_session", side_effect=fake_focus), \
             patch("claude_monitor._a_monitor_is_running", return_value=True):
            app = ClaudeMonitor()
            async with app.run_test(size=(160, 30)) as pilot:
                await settle(pilot)

                # --- t1: both working. Nothing needs you. -------------------
                assert cm.find_next_actionable(app.sessions, None, {}) is None
                assert app._bell == {}

                # --- t2: A finishes. READY, unseen, yellow, bold, no bell. ---
                a.status = "done"
                a.last_activity = 2000.0
                app.refresh_sessions()
                await settle(pilot)
                assert READY_COLOR_DARK in _status_cell(app, "a"), "t2: unseen READY is yellow"
                assert "[bold" in _status_cell(app, "a"), "t2: unseen badge is bold"
                assert "[bold]" in _title_cell(app, "a"), "t2: unseen title is bold"
                assert app._bell == {}, "t2: done never rings the bell"

                # --- Ctrl+Shift+N lands on A and marks it seen once. --------
                app._handle_jump_next_request()
                await settle(pilot)
                assert jumps == ["a"], "first jump goes to the one READY session"
                acked = cm._normalize_acked_ready(cm.load_prefs().get("acked_ready", {}))
                assert acked == {"a": [1, 2000.0]}, "ack persisted with A's last_activity stamp"
                app.refresh_sessions()
                await settle(pilot)
                assert READY_SEEN_COLOR in _status_cell(app, "a"), "seen once: mint"
                assert "[bold" in _status_cell(app, "a"), "seen once: badge still bold"
                assert "[bold]" not in _title_cell(app, "a"), "seen once: title unbold"

                # --- Ctrl+Shift+N again: nothing unseen, so it does NOT jump. -
                app._handle_jump_next_request()
                await settle(pilot)
                assert jumps == ["a"], "a seen session is not a candidate again"

                # --- A menu-jump (explicit) counts as a second look. ---------
                app._make_menu_handler(a)("jump")
                await settle(pilot)
                assert jumps == ["a", "a"]
                acked = cm._normalize_acked_ready(cm.load_prefs().get("acked_ready", {}))
                assert acked["a"][0] == 2, "second look increments, same episode"
                app.refresh_sessions()
                await settle(pilot)
                assert "[bold" not in _status_cell(app, "a"), "seen twice: badge unbolds too"
                assert "READY" in _status_cell(app, "a"), "seen twice: still says READY"

                # --- t3/t4: A does NEW work and finishes again. --------------
                # This is #51's bug: the ack must be void, A unseen again.
                a.status = "working"
                a.last_activity = 3000.0
                app.refresh_sessions()
                await settle(pilot)
                a.status = "done"
                a.last_activity = 4000.0
                app.refresh_sessions()
                await settle(pilot)
                assert READY_COLOR_DARK in _status_cell(app, "a"), \
                    "t4: new work since the ack => unseen/yellow again (was #51)"
                assert "[bold]" in _title_cell(app, "a"), "t4: title bold again"
                app._handle_jump_next_request()
                await settle(pilot)
                assert jumps == ["a", "a", "a"], "t4: Ctrl+Shift+N lands on A again"
                acked = cm._normalize_acked_ready(cm.load_prefs().get("acked_ready", {}))
                assert acked["a"] == [1, 4000.0], "t4: count restarted at 1 on the new episode"

                # --- B finishes too; the walk should go to B, not back to A. -
                b.status = "done"
                b.last_activity = 4500.0
                app.refresh_sessions()
                await settle(pilot)
                app._handle_jump_next_request()
                await settle(pilot)
                assert jumps[-1] == "b", "only B is unseen now"

                # --- Inbox mode on: hides nothing here (both READY) ----------
                await pilot.press("ctrl+b")
                await settle(pilot)
                assert {s.title for s in app._row_map if s} == {"alpha", "beta"}

                # --- t5: A needs approval while inbox is on: bell must ring. -
                a.status = "needs_approval"
                a.last_activity = 5000.0
                app.refresh_sessions()
                await settle(pilot)
                assert "a" in app._bell and app._bell["a"]["acked"] is False, \
                    "t5: APPROVE transition rings even under inbox mode"
                # ...and it is the top Ctrl+Shift+N candidate, seen or not.
                target = cm.find_next_actionable(
                    app.sessions, None,
                    cm._normalize_acked_ready(cm.load_prefs().get("acked_ready", {})))
                assert target is not None and target.session_id == "a"

                # --- A goes back to working (approved) and is hidden by inbox.
                a.status = "working"
                app.refresh_sessions()
                await settle(pilot)
                assert [s.title for s in app._row_map if s] == ["beta"]
                assert "a" not in app._bell, "bell clears when it stops needing approval"

                # --- A asks AGAIN while hidden: must ring again (review bug). -
                a.status = "needs_approval"
                a.last_activity = 6000.0
                app.refresh_sessions()
                await settle(pilot)
                assert "a" in app._bell, "second APPROVE after a hidden working spell still rings"

                # --- Shift+R semantics: clear every ack. ----------------------
                # (action_restart itself exits the app and runs git pull, so
                # exercise the same mutator it uses.)
                def _clear(prefs):
                    if not prefs.get("acked_ready"):
                        return False
                    prefs["acked_ready"] = {}
                    return True
                cm._update_prefs(_clear)
                assert cm.load_prefs().get("acked_ready") == {}
                b.last_activity = 4500.0  # unchanged, but its ack is gone now
                app.refresh_sessions()
                await settle(pilot)
                assert READY_COLOR_DARK in _status_cell(app, "b"), "after clear: B unseen again"

    async def test_prefs_file_survives_the_whole_run_intact(self):
        """State-leak canary for this file specifically: the scratch prefs
        file is the ONLY file the lifecycle may write, and the conftest
        fixture must have pointed us at it. If this ever reads the real
        path, the 2026-08-17 prefs-clobber incident is back."""
        assert "monitor-prefs.json" in str(cm.PREFS_PATH)
        assert ".claude" not in str(cm.PREFS_PATH), cm.PREFS_PATH
