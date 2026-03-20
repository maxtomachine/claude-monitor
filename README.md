# Claude Monitor

A btop-style terminal dashboard for monitoring Claude Code sessions in real-time, plus a custom two-line HUD statusline that lives inside every Claude Code session.

![Python](https://img.shields.io/badge/python-3.12+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## What it does

Claude Monitor watches your `~/.claude/projects/` session logs and displays a live-updating table of all active sessions. Built for power users running multiple Claude Code instances in parallel.

```
✻ WORKING   Build session monitor TUI    Opus 4.6   ██████████ 95%   245k   $3.42   0s   Editing claude_monitor.py
. WAITING   Delete empty Gmail drafts    Opus 4.6   ████░░░░░░ 38%   1.2M   $6.76   9m   Searched Gmail
◌ IDLE      Refactor auth middleware     Sonnet 4.6 ██░░░░░░░░ 15%   890k   $12.50  2h   Edited main.rs
```

### Features

- **Live session table** — auto-refreshes every 3 seconds
- **Kanban board** — `k` toggles a status-column board view with arrow navigation and animated spinners
- **Spinner animation** — working sessions show a ping-pong `·*✢✳✶✻` cycle; static icons for other statuses
- **Hook-based state tracking** — instant status updates via Claude Code hooks (no transcript polling needed)
- **Activity tracking** — "Doing" column shows what each session is up to (gerund when working, past tense when idle)
- **Context bar** — colored progress bar (green → yellow → red as context depletes)
- **Compaction tracking** — colored markers show how many times context was compacted
- **Cost estimation** — per-session cost based on model pricing and token counts
- **Subagent tree view** — expand sessions to see their subagents indented below
- **Jump to terminal** — instantly raise the Ghostty/Terminal window for any session
- **Send /rename** — patch unnamed sessions directly from the monitor
- **Resume fallback** — if a session's terminal can't be found, opens a new tab and resumes
- **Dark/light theme** — `t` toggles between dark and light, persisted to preferences
- **Sort modes** — cycle through: last active, status, context %, tokens, cost
- **Search/filter** — `/` to filter sessions by name, project, status, or model
- **Column picker** — toggle columns on/off, reorder with Shift+arrows, preferences saved to disk
- **Restart** — `R` restarts the monitor in-place (picks up code changes without losing terminal)

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `j` / arrows | Navigate sessions |
| `Enter` | Open action menu for selected session |
| `k` | Kanban board view |
| `s` | Cycle sort mode |
| `a` | Toggle subagent tree view |
| `z` | Show all sessions (including archived) |
| `c` | Open column picker |
| `t` | Toggle dark/light theme |
| `n` | Send /rename to selected session |
| `R` | Restart monitor |
| `/` | Search / filter |
| `Esc` | Clear search |
| `r` | Manual refresh |
| `d` | Toggle debug logging |
| `q` | Quit |

---

## The Statusline

A two-line HUD that lives inside every Claude Code session, showing context usage, effort/speed modes, and session costs at a glance.

```
ctx ██░░░░░░▒▒  22%  🧠 max   113k tok
·636519dc  ⚡ fast  Bash       24   $
```

### What each part shows

**Line 1 — Context window:**
- 10-block colored bar (green → yellow → red → blink red at 90%+)
- Effort level indicator (🧠) — only when `/effort` is set to non-auto
- Total token count

**Line 2 — Usage / hook state:**
- Session ID marker (·{sid8}) — used by jump-to-terminal matching
- Fast mode indicator (⚡) — reads live from settings.json
- Current tool name (from hook state)
- Session cost

### Color tiers (context bar)

| Usage | Color |
|-------|-------|
| < 50% | Green |
| 50-74% | Yellow |
| 75-79% | Red |
| 80-89% | Bold red |
| 90%+ | Blinking red |

---

## Install

One command sets up everything — monitor TUI, statusline, hooks, and launcher:

```bash
git clone https://github.com/maxtomachine/claude-monitor.git ~/Projects/claude-monitor
cd ~/Projects/claude-monitor
./install.sh
```

This will:
- Install `uv` and `jq` if missing
- Set up the Python environment (`uv sync`)
- Symlink the statusline into `~/.claude/statusline.sh`
- Install the session tracker hook at `~/.claude/hooks/session_tracker.py`
- Add `statusLine` and `hooks` config to `~/.claude/settings.json`
- Copy monitor column preferences to `~/.claude/monitor-prefs.json`
- Create a `claude-monitor` launcher at `~/.local/bin/claude-monitor`

Then restart Claude Code for the statusline and hooks, and run `claude-monitor` from anywhere for the TUI.

### Updating

Since the installer symlinks everything back to the repo, pulling is all you need:

```bash
cd ~/Projects/claude-monitor
git pull
```

No reinstall required.

---

## How it works

### Status detection (3-tier)

1. **Hook state** (instant) — hooks write JSON to `~/.claude/session-states/` on every event
2. **Signal files** (legacy) — `~/.claude/session-signals/` for simple working/stop/permission signals
3. **Timing heuristics** (fallback) — < 30s since last output = working, < 5min = waiting, else idle

### Session title resolution

1. User-set title (`/rename` → `custom-title` in transcript)
2. Hook-generated title (from `session-memory/summary.md` or Haiku API fallback)
3. Sessions-index summary
4. First user prompt
5. Working directory name

### Jump-to-terminal

The hook script writes the terminal title via `/dev/tty{N}` with a unique `·{sid8}` marker. The monitor matches this marker against Ghostty/Terminal window titles using a single JXA call with `byName()` lookup (z-order safe, ~200ms).

### Data sources

| Data | Source |
|------|--------|
| Session list | `~/.claude/projects/**/*.jsonl` (by mtime) |
| Status | Hook state files → signal files → timing heuristics |
| Context % | Ground-truth from statusline cache or token estimation |
| Tokens / cost | Accumulated from `usage` blocks in assistant messages |
| Activity | Hook state `tool` + `tool_target`, or transcript last tool call |
| Effort level | Transcript (`/effort` command) → `settings.json` fallback |
| Fast mode | `settings.json:fastMode` (live toggle state) |

---

## Configuration

Preferences are saved to `~/.claude/monitor-prefs.json`:

```json
{
  "columns": ["status", "session", "project", "model", "context", "compact", "tokens", "cost", "active", "doing"],
  "theme": "textual-dark"
}
```

### Column picker (`c`)

Toggle columns on/off and reorder them with Shift+arrows. Preferences persist across sessions.

| Column | Description | Default |
|--------|-------------|---------|
| Status | Working / Waiting / Idle with animated spinner | on |
| Session | Name or AI summary | on |
| Project | Project directory | on |
| Model | Opus 4.6, Sonnet 4.6, etc. | on |
| Context | Colored bar + percentage | on |
| Compacts | Markers for compaction count | on |
| Tokens | Total token count | on |
| Cost | Estimated $ spent | on |
| MCP | MCP tool call count | off |
| Msgs | Message count | off |
| Duration | Session lifetime | off |
| Active | Time since last activity | on |
| Doing | Current activity / last action | on |

---

## Testing

172 tests covering pure functions, rendering, transcript parsing, full TUI interactions, and end-to-end tmux tests.

```bash
uv sync --group dev
uv run pytest tests/ -v
```

| File | What it covers |
|------|----------------|
| `test_formatting.py` | format_model, format_tokens, format_cost, context bar, compactions |
| `test_gerunds.py` | Activity generation — gerund mapping, MCP tools, text extraction, past tense |
| `test_parsing.py` | Transcript parsing, timestamp handling, status detection, sorting, hook state, session-memory titles |
| `test_rendering.py` | Row rendering, column config, truncation, subagent display |
| `test_tui.py` | Full app: keybindings, session menu, column picker, search, subagents, kanban |
| `test_tmux_e2e.py` | Real TUI in tmux: startup, keybinding routing, theme toggle, kanban navigation (flaky, skipped in CI) |

---

## Contributing

```bash
git clone https://github.com/maxtomachine/claude-monitor.git
cd claude-monitor
git checkout -b my-feature

# Make changes, run tests
uv run pytest tests/ -v

# Push and open a PR
git push -u origin my-feature
gh pr create
```

---

Built with [Textual](https://textual.textualize.io/) and [Rich](https://rich.readthedocs.io/).
