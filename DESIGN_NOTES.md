
# claude-monitor UI rules (migrated from auto-memory 2026-05-25)

## substring_match_home_basename
---
name: Never use home-dir basename as substring match candidate
description: Path.home().name (= username) is dangerously broad for window-title substring matching — it matches any window containing the username, causing wrong-target jumps
type: feedback
---

When building a fallback chain of candidates for window-title substring matching, never include the home directory basename (`Path.home().name`). It's the user's login name and will match any window title containing that string — including other sessions' hook-set titles that embed the username.

**Why:** claude-monitor jump log analysis (2026-03-27): session `63345fae` had no `·sid8` marker (Claude clobbered it post-Stop). The candidate chain fell through to cwd basename `mk` (from `/Users/mk`), which substring-matched `✳ mk ·6cb05ee4` — a completely different session's window. User jumped to the wrong terminal twice before reporting it. The next three attempts returned `no_match` because the wrong-window focus had changed which windows were visible.

**How to apply:** In any title/name matching fallback chain:
- Check `if candidate == Path.home().name: skip` before adding cwd-derived candidates
- More broadly: any candidate shorter than ~4 chars or equal to common identifiers (username, hostname, "main", "src") should be rejected — the false-positive rate outweighs the catch rate
- Prefer failing to match (triggering a clean fallback like resume-in-new-tab) over matching the wrong target

## fallback_to_resume
---
name: Fallback to resume instead of failing — reconnect to orphaned sessions
description: When a monitor action (jump, rename, send command) can't find the target terminal window, don't just toast an error — fall back to opening a new terminal and resuming the session there
type: feedback
---

When an action that targets a specific terminal window fails to find it, fall back to opening a new tab and resuming the session rather than reporting failure.

**Why:** User asked for this when the jump-to-terminal and rename actions showed "Could not find terminal" for sessions whose windows existed but couldn't be matched. The session is still running — the user wants to interact with it, not be told it's unreachable. Resume reconnects to the existing session (not a duplicate), and the hooks fire immediately to set up tracking on the new tab.

**How to apply:** Any monitor action that sends commands to a terminal (jump, rename, debrief, custom commands) should follow the pattern: try `_send_to_terminal_session()` first, fall back to `resume_session()` on failure. The resume opens a Ghostty tab (cmd+T via JXA), types `cd {cwd} && claude --resume {sid}`, and returns. This is a general pattern for resilient session interaction — don't dead-end on window-matching failures.

## textual_theme_tokens
---
name: Textual CSS — use theme tokens not hardcoded rgba for modal backgrounds
description: Hardcoded rgba(0,0,0,X) in Textual modal CSS breaks light mode; use $background/$panel/$boost which auto-invert
type: feedback
---

When writing Textual ModalScreen CSS, use theme tokens instead of hardcoded rgba colors:
- `$background` — page background
- `$panel` — card/container backgrounds
- `$boost` — highlight/selected state
- `$primary-darken-2` (or `-lighten-2`) — borders that adapt direction

NOT `rgba(0, 0, 0, 0.7)` or `$surface-darken-3` — these don't invert in light mode.

**Why:** User said "light mode doesn't carry over to kanban view" after adding a theme toggle (2026-03-19). The modal's `background: rgba(0, 0, 0, 0.7)` was hardcoded black; `$surface-darken-2` and `$surface-lighten-1` kept their dark-mode direction. Swapping to `$background`, `$panel`, `$boost` fixed it — these tokens are defined per-theme and flip correctly.

**How to apply:** Any new ModalScreen or overlay in a Textual app that supports theme switching must use the semantic color tokens. If you need a translucent backdrop, use `$background 70%` not `rgba(0,0,0,0.7)`. Check all modals when adding a theme toggle — SessionMenu, ColumnPicker, etc. likely have the same hardcoded pattern copied.
