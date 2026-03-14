# Jump to Terminal — Bug Investigation File

## Status: UNSOLVED — Intermittent wrong-window targeting

## The Problem

"Jump to terminal" from the monitor's session menu should switch focus to the Ghostty window/tab running that specific Claude session. Instead, it consistently jumps to the **most recently active** Ghostty window, regardless of which session was selected.

Occasionally it works correctly (especially in scripted tests), but fails in live usage from the monitor TUI.

## Environment

- macOS (Darwin 25.3.0)
- Ghostty terminal (GPU-rendered, native macOS windows)
- Multiple Ghostty windows, some with tabs
- The monitor itself runs inside a Ghostty window
- Claude Code sets terminal titles to "⠂ Claude Code", "✳ Claude Code" etc. (status emoji + "Claude Code")

## What Works

- **PID lookup**: `_find_claude_pid()` correctly finds the Claude process for a session via:
  1. PID files in `~/.claude/sessions/<PID>.json`
  2. `lsof +D ~/.claude/tasks/{session_id}`
  3. Matching claude processes by session ID in their open files
- **Terminal detection**: `find_terminal_for_session()` correctly walks the process tree to identify "Ghostty"
- **AXRaise + proc.frontmost**: replaces the broken `tell application "Ghostty" to activate`. The old approach re-focused the monitor's own window since Ghostty was already the active app. The new approach correctly brings a specific window to front.
- **AXTextArea reading**: Ghostty exposes terminal text content via `wins[i].groups[0].groups[0].textAreas[0].value()`. This returns the full visible terminal buffer.
- **Tab switching**: `AXRaise` on a System Events "window" that represents a tab correctly switches Ghostty to that tab.
- **Content matching in scripted tests**: When run via `uv run python3 -c "..."`, the matching finds the correct window and raises it.

## What Fails

When the monitor TUI calls `focus_terminal_session()`, the JXA consistently picks the wrong window — typically the most recently active one. The debug log shows `no_match` even for sessions that should be matchable.

### Root Causes Identified

#### 1. System Events z-order instability
System Events `windows()` returns windows in z-order (front→back), NOT creation order. The z-order changes every time ANY window interaction occurs (including our own AXRaise calls). This means:
- The window indices shift between reads
- Between the first-pass scan and the second-pass tab switching, the order may change
- `wins[1]` in the first pass might be a different window than `wins[1]` in the second pass

#### 2. AXTextArea returns stale content after tab switch
After `AXRaise` on a tab + `delay(0.15)`, the `AXTextArea.value()` may not reflect the newly-active tab's content. The delay might be too short, or the accessibility tree might cache the old value. Observed: second-pass tab switching returns `no_match` even when the tab contains the match string.

#### 3. Session title ≠ Statusline session name
The monitor's `Session.title` comes from the transcript (custom_title, summary, firstPrompt, or cwd basename). The statusline's visible session name comes from Claude's live status JSON (`session_name`). These can differ, causing the match pattern `"title │"` to not find the right window.

#### 4. Stale slugs in the Session object
The `Session.slug` is extracted from the transcript JSONL. Sessions get new remote control URLs over time. The slug in the transcript may be an OLD URL, while the statusline shows the CURRENT URL. Even after fixing to take the LAST slug from the transcript, the slug can still be stale if the session reconnected after the last transcript write.

#### 5. Truncated slugs in narrow terminals
The statusline truncates long URLs at narrow terminal widths (e.g., `session_015xe5fXCkCeU7WCJ…` instead of `session_015xe5fXCkCeU7WCJSAb6hQ3`). Matching on the full slug fails.

#### 6. Duplicate session titles
Multiple sessions can have the same title (e.g., "maxkirby"). When two open windows both have "maxkirby │" in their statusline, the matcher picks whichever it finds first (z-order dependent).

#### 7. Monitor's window confuses the match
The monitor TUI displays session names in its table/detail panel. The session name pattern "claude-monitor │" could potentially match the monitor's own window if the session name appears in its content. (Mitigated by checking only the last 500 chars of the terminal text.)

## Approaches Tried

### Approach 1: TTY-based window index mapping (FAILED)
- Find claude PID → get TTY → map TTY to login process → map login process index to window index
- **Failed because**: System Events windows are in z-order, login processes are in creation order — the two orderings don't correlate

### Approach 2: CGWindowListCopyWindowInfo (FAILED)
- Tried using CGWindowID (creation-ordered) to build a stable window ordering
- **Failed because**: Ghostty doesn't expose window titles via CGWindowList (all `undefined`). Also, CGWindowList includes internal/decoration windows that don't map 1:1 to visible windows. And tabs complicate the mapping further.

### Approach 3: `tell application "Ghostty" to activate` (FAILED)
- The original approach: AXRaise a window then activate the app
- **Failed because**: `activate` sends an Apple Event asking Ghostty to come to front, but since the monitor runs in Ghostty too, it re-focuses the monitor's own window (the "key window")

### Approach 4: AXRaise + proc.frontmost (PARTIALLY WORKS)
- AXRaise the target window, then set `proc.frontmost = true` via System Events
- **Works**: correctly brings a specific window to front without re-focusing the monitor
- **Limitation**: need to identify WHICH window to raise

### Approach 5: Slug-based AXTextArea matching (FAILED)
- Read each window's AXTextArea text, search for `session_{slug}` from the session's remote URL
- **Failed because**: slugs in the Session object are stale (transcript has old URLs), and the statusline truncates long slugs

### Approach 6: Session name matching with `"name │"` pattern (INTERMITTENT)
- Match `"session_title │"` in the last 500 chars of each window's AXTextArea
- **Works in scripted tests**: correctly identifies windows
- **Fails in live monitor**: z-order shifts and/or AXTextArea staleness cause `no_match`
- Tab-switching second pass also fails (likely delay too short or AX tree caching)

## Key Observations from Debug Logs

```
# Works (scripted test):
13:18:22 match='claude-monitor │'  → matched:0:⠂ Claude Code

# Works then fails (live monitor — z-order shifted):
13:19:51 match='claude-monitor │'  → matched:1:✳ Claude Code  (WRONG window? or z-order changed)
13:20:01 match='maxkirby │'        → no_match (4 windows scanned, none matched)
13:20:35 match='claude-monitor │'  → no_match (4 windows scanned, none matched)
```

The `no_match` results are suspicious — the session name SHOULD be in the statusline. Possible explanations:
1. The AXTextArea content is stale (not refreshed since last tab switch)
2. The `│` character (U+2502) isn't matching due to encoding differences
3. The statusline `session_name` doesn't match the monitor's `Session.title`
4. The AXTextArea read fails silently and returns empty string

## Suggested Next Steps

### 1. Add verbose AXTextArea logging
Log the actual last 200 chars of text read from each window during the match attempt. This will reveal whether the text is stale, empty, or just doesn't contain the expected pattern.

### 2. Investigate AXTextArea refresh timing
After AXRaise on a tab, try longer delays (0.3s, 0.5s, 1.0s) and verify the content actually changes. Or use a polling approach: read, check, wait, re-read.

### 3. Compare session_name vs title
Add logging to show both the statusline's `session_name` (from Claude's status JSON) and the monitor's `Session.title` (from the transcript). These might differ.

### 4. Try window-ID-stable approach
Instead of using z-order-based `wins[i]`, try to use a stable window identifier:
- System Events windows might have a `.id()` property (threw an error in one test)
- CGWindowID is stable but hard to correlate with SE windows
- Window position is stable (doesn't change with z-order) — could use position as a key

### 5. Try Ghostty-specific IPC
Check if Ghostty has a CLI command, socket, or config-based mechanism for focusing specific windows/tabs. This would bypass the entire Accessibility API approach.

### 6. Try AppleScript `set index of window` approach
Instead of AXRaise, try `tell application "Ghostty" to set index of window X to 1`. Some apps support this for window ordering.

### 7. Pre-compute window mapping on refresh
During `refresh_sessions()`, proactively scan all windows and build a session_id → window_position mapping. Store this mapping and use positions (which are stable) instead of z-order indices for raising.

### 8. Consider a completely different approach
Instead of trying to raise the right window from the monitor, write the target session ID to a temp file and have a Ghostty keybinding/hook read it and switch to the right tab. This decouples the window management from the Python process.

## Relevant Code Locations

- `claude_monitor.py`:
  - `_find_claude_pid()` — PID lookup (works)
  - `find_terminal_for_session()` — terminal app detection (works)
  - `_raise_window_by_content()` — window matching + raise (intermittent)
  - `focus_terminal_session()` — orchestrator
- `statusline/statusline.sh` — writes context/URL cache files to `/tmp/claude-*`
- Debug log: `/tmp/claude-monitor-jump.log`
