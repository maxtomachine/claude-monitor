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

### READY has a read/unread state

Jumping to a `done` session (via the menu, double-click, or `Ctrl+Shift+N`)
without sending it anything acknowledges it: the status cell stays bold and
still says READY, but moves off `bright_yellow` to a mint (`READY_SEEN_COLOR`,
`#8FD9B6`), and the row title unbolds. It's the same distinction email gives
read mail, applied to "have you already glanced at this one" rather than "is
it actually still waiting on you" (Max, 2026-08-16: "a sort of read/unread
filter"). First cut kept the status yellow and only unbolded the row, then
Max asked for the color itself to change, then clarified it should stay bold
doing so ("it can still be bold and ready but not yellow"); the version
above is the one that shipped.

The acknowledgment (`_mark_ready_seen()`) is persisted to `monitor-prefs.json`
under `acked_ready`, not held in memory, because `Ctrl+Shift+N` runs as its
own short-lived process outside the running monitor and can only tell it
about a jump by writing it to disk. Only an *actual* jump marks a session
seen (every jump call site checks the jump/resume actually succeeded before
calling `_mark_ready_seen()`, not just attempted it). Plain `n` (below)
never does, on purpose: it's pure cursor navigation, not a visit.

`_mark_ready_seen()` is the ONLY writer of `acked_ready`. The refresh cycle
reads it fresh each pass (so a jump from a separate `--jump-next` process is
picked up) and filters it to currently-`done` sessions for rendering, but
that filtering must stay purely in-memory. An earlier version also wrote the
filtered copy back to disk on every ~3s refresh, and that write raced
`_mark_ready_seen()`'s own read-modify-write: a jump landing mid-refresh-cycle
set the seen mark, and the refresh cycle's write (holding a prefs snapshot
read before that jump) clobbered it back to unseen a moment later, repeating
on every following cycle (Max, 2026-08-16: "my 'marked as read' doesn't seem
to stay that way... they are turning yellow on me" / "turned yellow multiple
times after a brief time as mint green"). If you ever need the refresh path
to write this field again, don't: give it its own single-writer discipline
instead of reintroducing a second read-modify-write on the same key.

Seen also means "don't make me look at this again": `find_next_actionable()`
(the shared selection behind `Ctrl+Shift+N`, not the `n` key, which has its
own table-order selection) takes an `acked_ready` set and excludes any
already-seen `done` session from its candidates entirely, not just from
sort priority. Without this, the hotkey kept landing back on a session Max
had already jumped to and decided to defer, on every press, which defeated
the whole point of tracking seen state (Max, 2026-08-17: "I don't want
ctrl-shift-n to jump me to an already read, redundantly... I already
jumped and took no action" / "I am wasting time checking things I already
decided to deal with later"). `needs_approval` is never excluded this way:
`acked_ready` only ever holds `done` sessions, and a blocking approval
request stays urgent regardless of whether it's been looked at. If every
actionable session happens to already be seen, this correctly returns
`None` ("nothing needs you") rather than re-visiting one anyway.

**Test isolation for anything under `monitor-prefs.json` (or any other
on-disk state this file owns) is not optional.** `tests/conftest.py`'s
`_isolate_monitor_state_files` autouse fixture points `PREFS_PATH`,
`HIDDEN_PATH`, `PINNED_PATH`, `SCAN_CACHE_PATH`, and `JUMP_REQUEST_PATH` at
per-test scratch files for every test, specifically because a test that
mocks `load_prefs()` to return a stub dict but forgets to also mock
`save_prefs()` will otherwise write that stub back to Max's real file for
real: `on_mount()`'s launch-count tracking calls both on every app
mount, so ANY test that spins up `ClaudeMonitor()` and mocks one without
the other clobbers real state, not just the field the test cares about.
This happened for real on 2026-08-17: one test mocking `load_prefs` to
return `{"acked_ready": [...]}` without also mocking `save_prefs` wiped
Max's saved `columns`/`column_order`/`view_state` down to just that
stub. Do not remove or narrow the autouse fixture to "fix" a test that
seems to want the real files; give it its own explicit scratch path
instead.

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

## Ctrl+Shift+N hands off to the running monitor instead of cold-scanning

`claude-monitor --jump-next` used to call `parse_sessions()` itself, cold, in
a brand-new process. Profiled 2026-08-16 against 136 real sessions: ~6.5s,
almost entirely `scan_full_file()` re-parsing every transcript's JSONL from
scratch with no cache warm from a prior run. That is the actual cause of
"the hotkey is working but it's slow" (Max, 2026-08-16), not `focus_terminal_
session()` (~0.3s) or anything else downstream.

Fix: hand the request off to whichever monitor is already running instead.
`_try_fast_jump_next()` drops the sentinel `__jump_next__` into the same
`/tmp/claude-jump-request` file the click-to-jump HTTP listener already
uses, which the running monitor's existing 200ms poller
(`_check_jump_request`) picks up and serves from `self.sessions`, already
warm from that instance's own 3-second refresh loop. Measured live: ~0.24s
end to end. The cold `jump_to_next_actionable()` scan is now only a
fallback for the case no monitor is running at all (rare in practice, since
the whole point of this app is to have one open); a `--restart` flag drops
a matching `__restart__` sentinel so a live monitor can be told to pick up
code changes without a synthetic keystroke into its window (see the Nest
warning below).

`_pid_is_claude()` used to spawn its own `ps -p <pid>` subprocess per call;
236 such calls cost ~1.5s of that same cold scan. Now backed by one
`ps -Ao pid=,comm=` snapshot per 2 seconds (`_refresh_process_comm_cache()`,
mirroring the existing `_refresh_pid_map()` pattern), looked up in memory
per PID instead.

The fast-path handoff above only helps when a monitor is already running.
The remaining ~5s of that cold scan (mostly `scan_full_file()`'s per-line
`json.loads`) still hit on a fresh launch or a `Shift+R` restart, since
`_scan_cache` starts empty in every new process regardless. Fixed
2026-08-17 (Max: "performance improve") by persisting `_scan_cache` to
`monitor-scan-cache.json`, loaded once per process
(`_load_scan_cache_from_disk()`) and flushed once per `parse_sessions()`
call when anything actually changed (`_scan_cache_dirty`), not per
session. Measured live: a fresh process's `parse_sessions()` against
mostly-unchanged transcripts dropped from ~4.5s to ~0.3s once a prior run
had already scanned them, roughly the same order of magnitude as the
Ctrl+Shift+N fix above, for the case that fix doesn't cover.

## Current keybindings

Plain letters (no modifier) are reserved for the type-ahead group jump below, so every command sits on `Ctrl+letter` instead. `K`/`R`/`P` were already Shift-bound and don't collide with a bare letter. `n` is a fourth, deliberate exception (Max, 2026-08-16), at the cost of no group name starting with "n" being reachable via type-ahead. It was first built to jump immediately, single-keystroke; Max caught and reversed that ("n in monitor should just move the highlighted row... n-Enter-Enter would also jump but not just naked n") — plain `n` only moves the cursor, `Ctrl+Shift+N` is the actual jump command, usable from inside the monitor or anywhere else on the machine.

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
| `n` | Move the cursor to the next session that needs you (needs_approval, then done, oldest-waiting first), cycling from wherever the cursor is now. Doesn't jump: follow with `Enter` → pick Jump to actually go there |
| `Ctrl+Shift+N` (global, via `skhd`) | Instantly jump to the next session that needs you, from anywhere on the machine, no monitor window shown — `claude-monitor --jump-next`. Shares a persisted queue position (`next_cursor` in `monitor-prefs.json`) so repeated presses walk the whole queue rather than repeating |
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
