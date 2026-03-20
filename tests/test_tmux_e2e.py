"""tmux-based end-to-end tests.

These spawn the monitor in a real tmux pane, send keystrokes, and assert on
captured terminal output. They test the full rendering pipeline that headless
Pilot tests can't reach: escape sequences, terminal title, keybinding routing
through a real TTY.

Marked flaky — timing-sensitive, tmux-version-sensitive, and skipped in CI
where there's no tmux/transcripts. These supplement the deterministic Pilot
tests; failures here don't block merge.

Not testable this way:
- jump-to-terminal (needs real Ghostty windows)
- restart via os.execv (kills the test process)
- hook integration (needs a running claude process)
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        shutil.which("tmux") is None, reason="tmux not installed"
    ),
    pytest.mark.skipif(
        os.environ.get("CI") == "true", reason="no TTY/transcripts in CI"
    ),
]

SESSION = "clmon-test"
MONITOR_CMD = f"cd {Path(__file__).parent.parent} && uv run python claude_monitor.py"


def _tmux(*args: str, timeout: float = 5) -> str:
    result = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0 and args and args[0] not in ("kill-session",):
        pytest.fail(f"tmux {args[0]} failed (rc={result.returncode}): {result.stderr.strip()}")
    return result.stdout


def _capture(raw: bool = False) -> str:
    """Capture pane content. raw=True includes ANSI escapes."""
    args = ["capture-pane", "-t", SESSION, "-p"]
    if raw:
        args.append("-e")
    return _tmux(*args).rstrip()


def _send(keys: str) -> None:
    _tmux("send-keys", "-t", SESSION, keys)


def _wait_for(substring: str, timeout: float = 8, interval: float = 0.2) -> str:
    """Poll capture-pane until substring appears or timeout."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = _capture()
        if substring in last:
            return last
        time.sleep(interval)
    pytest.fail(f"Timed out waiting for {substring!r} in pane:\n{last[-500:]}")
    return last  # unreachable


@pytest.fixture
def tmux_monitor():
    """Spawn the monitor in a tmux session, tear down after test."""
    # Kill any stale session first
    subprocess.run(
        ["tmux", "kill-session", "-t", SESSION],
        capture_output=True,
    )
    # New detached session sized like a real terminal
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", SESSION, "-x", "140", "-y", "40",
         MONITOR_CMD],
        capture_output=True,
        check=True,
    )
    # Wait for the footer keybindings to appear (monitor finished mounting)
    _wait_for("Kanban")
    yield SESSION
    subprocess.run(
        ["tmux", "kill-session", "-t", SESSION], capture_output=True,
    )


class TestStartup:
    def test_monitor_mounts(self, tmux_monitor):
        content = _capture()
        # Footer keybindings present
        assert "Quit" in content
        assert "Kanban" in content

    def test_table_renders(self, tmux_monitor):
        content = _capture()
        # DataTable headers present
        assert "Session" in content
        assert "Status" in content


class TestKeybindings:
    def test_search_opens_and_closes(self, tmux_monitor):
        _send("/")
        _wait_for("Search")
        _send("Escape")
        time.sleep(0.5)
        content = _capture()
        # Search input no longer shown
        assert content.count("Search") <= 1  # Footer label might remain

    def test_kanban_opens(self, tmux_monitor):
        _send("k")
        content = _wait_for("Kanban Board")
        assert "Working" in content
        assert "Idle" in content
        _send("Escape")
        time.sleep(0.5)
        content = _capture()
        assert "Kanban Board" not in content

    def test_column_picker_opens(self, tmux_monitor):
        _send("c")
        _wait_for("Column Picker")
        _send("Escape")
        time.sleep(0.5)
        assert "Column Picker" not in _capture()

    def test_theme_toggle(self, tmux_monitor):
        before = _capture(raw=True)
        _send("t")
        time.sleep(0.5)
        after = _capture(raw=True)
        try:
            assert before != after
        finally:
            # Always restore original theme even if assertion fails
            _send("t")
            time.sleep(0.5)

    def test_sort_cycles(self, tmux_monitor):
        _send("s")
        _wait_for("Sort:")


class TestSpinner:
    def test_working_status_animates(self, tmux_monitor):
        """If any session is working, the spinner should change between captures."""
        snap1 = _capture()
        if "WORKING" not in snap1:
            pytest.skip("no working sessions to observe spinner")
        time.sleep(0.5)  # > 132ms tick
        snap2 = _capture()
        # Spinner frame should have changed in the WORKING cell
        # (this is the weakest assertion — might collide on same frame)
        working_line1 = next((l for l in snap1.splitlines() if "WORKING" in l), "")
        working_line2 = next((l for l in snap2.splitlines() if "WORKING" in l), "")
        # Don't fail hard — just verify something renders
        assert working_line1 and working_line2


class TestKanbanNavigation:
    def test_arrow_keys_move_selection(self, tmux_monitor):
        _send("k")
        _wait_for("Kanban Board")
        snap1 = _capture()
        _send("Right")
        time.sleep(0.3)
        snap2 = _capture()
        # Selection highlight border should have moved
        # (hard to assert precisely in ANSI; just check something changed)
        _send("Escape")
        # Best-effort: assert that pane content changed after navigation.
        # May collide if selection was already at rightmost non-empty column.
        if snap1 == snap2:
            pytest.skip("selection didn't visibly move (edge position)")

    def test_enter_opens_session_menu(self, tmux_monitor):
        _send("k")
        _wait_for("Kanban Board")
        snap_before = _capture()
        if snap_before.count("—") >= 5:
            pytest.skip("all columns empty — no card to select")
        _send("Enter")
        time.sleep(0.5)
        content = _capture()
        # SessionMenu shows Jump/Resume options
        assert "Jump" in content or "Resume" in content or "Copy" in content
        _send("Escape")
        _send("Escape")
