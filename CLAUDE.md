# Claude Monitor — Development Guide

## Quick Start

```bash
uv sync --group dev    # install all deps including test tools
uv run pytest tests/ -v  # run the full test suite
```

## Testing

**Always run tests before committing.** When you add new features, add corresponding tests and run the suite to catch breakage early.

```bash
uv run pytest tests/ -v
```

### Test structure

- `tests/test_formatting.py` — pure formatting functions (format_model, format_tokens, etc.)
- `tests/test_gerunds.py` — activity/gerund generation for the "Doing" column
- `tests/test_parsing.py` — transcript parsing, status detection, sorting
- `tests/test_rendering.py` — row rendering, column config, truncation
- `tests/test_tui.py` — full TUI integration tests using Textual's headless pilot

### Writing TUI tests

TUI tests use Textual's `run_test()` to mount the app headlessly. Key patterns:

- Mock `parse_sessions` to control session data: `patch("claude_monitor.parse_sessions", return_value=sessions)`
- Always `await pilot.pause()` after keypresses that trigger UI changes
- Query modal screens via `pilot.app.screen.query_one(...)`, not `pilot.app.query_one(...)`
- Use `tests/helpers.py` for `make_session()` and `make_transcript_jsonl()` factories

### Writing unit tests

Pure functions can be tested directly — no async needed. Import from `claude_monitor` and assert.

## Architecture

Single file: `claude_monitor.py`. Key sections:

1. **Constants & mappings** — model pricing, MCP service names, gerund lookups
2. **Data parsing** — `scan_full_file()` does a single-pass JSONL parse, `parse_sessions()` discovers and builds all sessions
3. **Formatting** — pure functions for tokens, cost, context bar, compactions, gerunds
4. **Terminal focus** — `find_terminal_for_session()` walks the process tree to find the owning terminal app
5. **Screens** — `SessionMenu` (action menu on Enter), `ColumnPicker` (column toggle + reorder)
6. **Main app** — `ClaudeMonitor(App)` with keybindings, refresh loop, search, sort

## Current keybindings

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Refresh |
| `s` | Cycle sort mode |
| `a` | Toggle subagent rows |
| `z` | Toggle archived/resumable sessions |
| `c` | Column picker |
| `/` | Search/filter |
| `j`/`k` | Vim navigation |
| `Enter` | Session context menu |

## Statusline integration

The statusline (`statusline/statusline.sh`) shares data with the monitor:
- **Context %**: statusline writes ground-truth `remaining_percentage` to `/tmp/claude-ctx-{session_id}`, which the monitor reads instead of estimating from token counts.
- **Session name**: statusline writes `session_name` to `/tmp/claude-name-{session_id}`, used by jump-to-terminal for match resolution.
- **Jump to terminal**: matches on Ghostty window titles (instant, no AXTextArea cycling). Falls back to AXTextArea content for unrenamed sessions. Uses `AXRaise` + `AXMain` + `proc.frontmost` (not `tell app to activate`).
- **Structured log**: `~/.claude/monitor.log` — JSON-lines with categories (jump, status, signal, close, error). Auto-rotates at 5 MB. Tail with `uv run python claude_monitor.py --log` or `uv run python claude_monitor.py --log jump` to filter by category.
- **Debrief auto-close**: `/debrief` writes `/tmp/claude-debrief-done-{sid}`, monitor polls on each refresh and closes the terminal tab.

## Careful edits — avoid accidental regressions

This project layers multiple technologies in unusual ways: bash statusline scripts, JXA (JavaScript for Automation) embedded in Python, TUI rendering, and macOS Accessibility APIs. These aren't standard application patterns, so there's no muscle memory for how changes ripple.

**The core failure mode**: when modifying one aspect of a feature (e.g., making a bar width responsive), it's easy to silently break an adjacent feature that was working fine (e.g., the quota ammo bar disappearing). This happens because:

1. **Variable ordering matters in shell scripts.** The statusline is a single-pass bash script. Moving a reference to `$tw` into an earlier section without moving its definition too silently produces empty-string comparisons — no error, just wrong behavior. Always trace where a variable is defined before referencing it in a new location.

2. **No type system or compiler catches these.** Bash, JXA, and ANSI escape sequences are all stringly-typed. A broken bar doesn't throw — it just renders nothing or renders wrong. The only test is visual inspection.

3. **The blast radius is invisible.** Editing the context bar section can break the quota bar section 100 lines away because they share the same responsive variable. Editing a JXA window-raise script can break tab switching because z-order changes between reads.

**Before editing any section**, read the full surrounding context to understand what else depends on the same variables, ordering, or state. After making changes, visually verify ALL parts of the statusline or TUI — not just the part you changed.

## Key conventions

- **No direct pushes to main** — all changes go through PRs
- **Python 3.14+** — uses modern syntax (union types, etc.)
- **Dependencies**: `textual` for TUI, `rich` for markup. Dev: `pytest`, `pytest-asyncio`
- **Preferences** saved to `~/.claude/monitor-prefs.json` — columns and column order
- **Statusline** at `statusline/statusline.sh` — symlinked to `~/.claude/statusline.sh` by installer

## Common tasks

### Adding a new column

1. Add entry to `ALL_COLUMNS` dict with label and default visibility
2. Add rendering logic in `render_row()` under the new column key
3. Add data source in `scan_full_file()` and/or `build_session()` if needed
4. Add tests in `test_rendering.py`
5. Run `uv run pytest tests/ -v`

### Adding a new keybinding

1. Add `Binding(...)` to `ClaudeMonitor.BINDINGS`
2. Add `action_*` method on `ClaudeMonitor`
3. Add TUI test in `test_tui.py` using pilot keypresses
4. Run `uv run pytest tests/ -v`

### Adding MCP service support

1. Add service name mapping to `MCP_SERVICE_NAMES`
2. Add any action-specific gerunds to `MCP_ACTION_GERUNDS`
3. Add test in `test_gerunds.py`
4. Run `uv run pytest tests/ -v`
