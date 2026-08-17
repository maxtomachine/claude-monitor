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
- `tests/test_tmux_e2e.py` — tmux-based e2e tests (spawn real TUI in tmux pane, send keystrokes, assert on terminal output). Flaky by nature — skipped in CI and when tmux not installed. Supplements Pilot tests; failures don't block merge. Run: `uv run pytest tests/test_tmux_e2e.py -v`

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

## Status is two states, deliberately

`determine_status()` used to try to distinguish working/waiting/idle/background
via a four-tier fallback stack with 5-minute decay timers reconstructed from
polled hook files. A real incident held a closed session "working" for 4.5
hours on a stale hook (EBC-Shell, 2026-06-04), and in practice the finer
distinctions didn't tell Max anything he'd act on: he'd stopped watching the
monitor for that and used macOS's own native notifications instead, fired
directly from inside the running Claude process rather than reconstructed.

Collapsed (2026-08-15) to what's actually reliable: `needs_approval` (set the
instant the `PermissionRequest` hook fires, real and event-driven, no decay)
and everything else, reported as `done <elapsed>` via `format_ago` on
`last_activity` rather than bucketed into idle/waiting/background categories.
Background subagent/workflow activity folds into `working` (a session running
something in the background is still busy) rather than its own status; the
count still surfaces via `s.background_count` in the Doing column. The bell
(`self._bell`, the pulsing `●` that flags a row) now rings on `needs_approval`
transitions only, not the old noisy "waiting" transition every session passed
through constantly.

Column defaults changed to match: `duration` moved from on to off (`session`,
`status`, `doing` are the only defaults now). `doing` is fact-derived from
the transcript's last real tool call, not a guess, so it earns its default
slot in a way the old inferred sub-statuses never did.

## Pins are permanent; hiding inactive ones is a separate toggle

A pin is an unconditional exemption from every age filter in `parse_sessions()`,
by design: it stays until you unpin it, full stop (Max: "pins should stay
until I unpin them"). An earlier version of this made pins auto-expire after
7 days instead, which Max corrected: he wanted the simpler thing, an on/off
filter, not an age-based decay he'd have to reason about. `Ctrl+O`
(`hide_inactive_pins`, off by default) hides a pinned-but-inactive session
from the default view without touching the underlying pin at all; unpin it
or flip the toggle back to see it again. `Ctrl+z` history mode still shows
everything regardless of this toggle.

"Inactive" is exactly `render_row()`'s own bold/dim test (`status in
("archived", "closed")`, Max: "the same that makes a row not bold"), not
"process not running right now": two real bugs came from getting this
narrower. First cut only matched `status == "closed"`, missing `"archived"`
entirely, which is what most real pins age into (`is_archived` in
`parse_sessions()` relabels anything not touched in the last day, completely
independent of whether the pin is otherwise fine), so the toggle only ever
caught a handful of the actual clutter. Fixing that by widening the *base*
"hide closed sessions" filter to also exclude unpinned archived sessions was
its own regression: archived sessions were never hidden by default at all,
pinned or not, and that filter must stay untouched for anyone unpinned. The
`hide_inactive_pins` exclusion has to be a separate clause layered on top of
the original filter, not a rewrite of it. And a session pinned across many
short-lived terminal sessions (e.g. config-MCPs, closed at any given moment
but resumed routinely) is still active work even while its process happens
to not be running right now, which is exactly why the criterion is the same
status check the UI already uses, not a liveness check of its own.

## Current keybindings

Plain letters (no modifier) are reserved for the type-ahead group jump below, so every command sits on `Ctrl+letter` instead. `K`/`R`/`P` are the three exceptions: they were already Shift-bound and don't collide with a bare letter.

`Ctrl+h` and `Ctrl+i` are NOT usable bindings: the plain xterm input protocol Textual speaks encodes them identically to Backspace and Tab (no Kitty/enhanced-keyboard negotiation in `drivers/linux_driver.py`), so a binding on either is unreachable, not merely buggy. History and the preview panel sit on `z`/`v` instead. `Ctrl+s`/`Ctrl+q` were briefly suspected of the same problem (they're the classic XON/XOFF flow-control bytes) but `linux_driver.py` explicitly clears `IXON`/`IXOFF`, and both work; a failure to fire is a symptom of testing nested inside tmux, not a real collision.

| Key | Action |
|-----|--------|
| `letter` (typed in sequence) | Type-ahead jump to a group by name, like Finder/Explorer find (e.g. `s` `t` `r` `a` → strategy) |
| `Ctrl+q` | Quit |
| `Ctrl+r` | Refresh |
| `Ctrl+s` | Cycle sort mode |
| `Ctrl+a` | Toggle subagent rows |
| `Ctrl+z` | Toggle archived/resumable sessions (history mode) |
| `Ctrl+c` | Column picker |
| `Ctrl+g` | Toggle grouping |
| `Ctrl+v` | Toggle the preview/detail panel |
| `/` | Search/filter (typing filters; `↓` drops into the table keeping the filter; `Esc` clears) |
| `K` | Add/edit Anthropic API key (for haiku session summaries) |
| `R` | Restart monitor (picks up code changes) |
| `Ctrl+j` | Cursor down |
| `Ctrl+n` | Send `/rename` to selected session |
| `Ctrl+p` | Pin/unpin session (a pin never expires on its own) |
| `Ctrl+o` | Hide/show pinned-but-inactive (archived or closed) sessions in the default view (the pin itself is untouched either way) |
| `P` | Broadcast `/proactive` to all sessions in cursor's group |
| `PageUp` / `PageDown` | Jump to previous/next group header |
| `Home` / `End` | Jump to first/last row |
| `Enter` | Session context menu |
| `double-click` | Jump to session's terminal (single-click highlights only) |
| `Shift+Up/Down` | Extend multi-row selection |
| `Shift+Click` | Extend selection to clicked row |
| `Delete` / `Backspace` | Hide archived/closed row(s) — press twice to confirm (history mode only) |

Naming a session with `&ignore` (case-insensitive, anywhere in the name) opts it out of monitoring entirely: dropped from every view along with its PID-siblings and subagents, beats pinning. Implemented in `filter_ignored()`; remove the marker to track again.

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

5. **Never send System Events keystroke/keyCode to open a new window/tab.** Confirmed live 2026-08-15: any Accessibility-injected keyboard event, real key or fake modifier, any combo (Cmd+T, Cmd+N tested), fires Claude Nest's push-to-talk hotkey as a side effect, even from a plain terminal `osascript` with no claude-monitor involved at all. It is not about which key is sent; it is about the event being synthetic. `resume_session()` opens the window through Ghostty's own `newWindow({withConfiguration: {command: ...}})` scripting instead, confirmed clean against the same live reproduction. If a future change needs to simulate typing into a terminal app again, retest against Nest first.

4. **Any action that calls `refresh_sessions()` must preserve cursor position.** The refresh path restores the cursor by `_selected_key` (the sid under the cursor when refresh was scheduled). If your action removes/filters/re-sorts rows such that the cursor's sid is no longer in the new table, the cursor silently resets to row 0. Before calling `refresh_sessions()`, move the cursor to a row that will survive the refresh — or set `_selected_key` to a survivor's sid directly.

## Key conventions

- **No direct pushes to main** — all changes go through PRs
- **Python 3.12+** (venv is 3.12.13, `requires-python>=3.12`): modern syntax (union types, etc.) is fine; 3.14-only syntax is not
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
