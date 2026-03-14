# Claude Monitor — Roadmap

## In Progress

### Detail panel improvements
- Add 2 more lines of height real estate (min-height 5, max-height 35%)
- Make scrollable (overflow-y: auto) so long plans/transcripts are navigable
- Already implemented, needs testing at various terminal sizes

### Context % sync
- Statusline writes ground-truth `remaining_percentage` to `/tmp/claude-ctx-{session_id}`
- Monitor reads this instead of estimating from `last_input_tokens / 200000`
- Eliminates the persistent mismatch between statusline and monitor context display

### Jump to terminal
- Debug logging to `/tmp/claude-monitor-jump.log`
- Fixed: uses `AXRaise` + `AXMain` + `proc.frontmost` instead of `tell app to activate`
- Known limitation: can't reliably map TTY→window when multiple Claude windows exist (System Events returns z-order, not creation order). Current heuristic: skip frontmost window, pick first "Claude Code" titled window.
- Needs real-world testing

## Planned

### Air Traffic Controller — Session Hierarchy

The big vision: turn the monitor into an orchestration layer where Claude sessions can be organized into hierarchies with defined relationships.

**Core concept**: any Claude session can be "tucked" (indented one level) under another, establishing a parent-child relationship. The relationship type determines how the sessions interact.

**Relationship model** — a 2x2 framework based on two axes:

|  | Act (do work) | Inform (share context) |
|--|--------------|----------------------|
| **Upstream (parent → child)** | **Delegate**: parent directs child's work. Child executes tasks on behalf of parent. Full subordination — "you are my worker." | **Consult**: parent shares context/asks questions. Child provides information/opinions but doesn't take orders. "What do you think about X?" |
| **Downstream (child → parent)** | **Accountable**: child reports results back. Parent is the authority. Child says "here's what I did, approve?" | **Inform**: child sends FYI updates. Parent is a passive observer. "Just letting you know I did X." |

Inspired by RACI but adapted for AI orchestration. The key insight: relationships are defined by **direction** (who initiates) and **mode** (action vs information), giving four natural relationship types.

**Implementation considerations**:
- Indentation in the TUI (1 level) to show parent-child visually
- Context menu option to "tuck" one session under another
- Relationship type selector (delegate/consult/accountable/inform)
- Mechanism for cross-session communication (likely via shared files or MCP)
- How to pass context between sessions without blowing up context windows
- Session hierarchy persistence (survives monitor restart)
- Visual indicators for relationship type in the session row

**Open questions**:
- How does a "delegate" relationship actually work mechanically? Options: write instructions to a shared file, use `--resume` with injected context, MCP tool that reads from another session's transcript
- Should hierarchy be stored in the monitor's prefs, or in a shared location that survives across machines?
- How deep can nesting go? Start with 1 level, but the model supports arbitrary depth
- What happens when a child session archives? Does it detach from the parent?

### Other planned features
- Sort verification with 3+ sessions (sort works but isn't visually obvious with only 2 rows)
- Statusline narrow-width testing (responsive layout implemented, needs edge case testing)
- Archived session resume flow testing
