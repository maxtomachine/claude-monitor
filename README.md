# Claude Monitor

A btop-style terminal dashboard for monitoring Claude Code sessions in real-time.

![Python](https://img.shields.io/badge/python-3.14+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## What it does

Claude Monitor watches your `~/.claude/projects/` session logs and displays a live-updating table of all active sessions. Built for power users running multiple Claude Code instances in parallel.

```
● WORKING   Build session monitor TUI    maxkirby   Opus 4.6   ██████████ 95%   —   245k   $3.42   0s   Editing claude_monitor.py
○ WAITING   Delete empty Gmail drafts    maxkirby   Opus 4.6   ████░░░░░░ 38%   ✻✻  1.2M   $6.76   9m   Searching Gmail
◌ IDLE      Refactor auth middleware     Projects   Sonnet 4.6 ██░░░░░░░░ 15%   ✻✻✻ 890k   $12.50  2h   Edited main.rs
```

### Features

- **Live session table** — auto-refreshes every 3 seconds
- **Activity tracking** — "Doing" column shows what each session is up to (gerund when working, past tense when idle)
- **Context bar** — colored progress bar based on last API call input tokens (green → yellow → red as context depletes)
- **Compaction tracking** — colored ✻ markers show how many times context was compacted
- **Cost estimation** — per-session cost based on model pricing and token counts
- **MCP tool call counts** — see which sessions are heavy MCP users
- **Subagent tree view** — expand sessions to see their subagents indented below
- **Detail panel** — shows last assistant message preview for the highlighted session
- **Session actions** — Enter on any session for: jump to terminal, copy ID, open remote control, reveal transcript
- **Multi-terminal support** — jump to terminal works across Ghostty, iTerm2, Terminal, Kitty, Alacritty, WezTerm, and Warp
- **Sort modes** — cycle through: last active, status, context %, tokens, cost
- **Search/filter** — `/` to filter sessions by name, project, status, or model
- **Column picker** — toggle columns on/off, reorder with Shift+↑↓, preferences saved to disk
- **Pause all** — send SIGINT to all working Claude Code processes
- **Remote control URLs** — open `claude.ai/code/session_*` links directly

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `j` / `k` / ↑ / ↓ | Navigate sessions |
| `Enter` | Open action menu for selected session |
| `s` | Cycle sort mode |
| `t` | Toggle subagent tree view |
| `c` | Open column picker (Enter/Space toggle, Shift+↑↓ reorder, `s` save) |
| `p` | Pause all working sessions |
| `/` | Search / filter |
| `Esc` | Clear search |
| `r` | Manual refresh |
| `q` | Quit |

## Install

One command sets up everything — monitor TUI, statusline, launcher, and preferences:

```bash
git clone https://github.com/maxtomachine/claude-monitor.git ~/Projects/claude-monitor
cd ~/Projects/claude-monitor
./install.sh
```

This will:
- Install `uv` and `jq` if missing
- Set up the Python environment (`uv sync`)
- Symlink the statusline into `~/.claude/statusline.sh`
- Add `statusLine` config to `~/.claude/settings.json`
- Copy monitor column preferences to `~/.claude/monitor-prefs.json`
- Create a `claude-monitor` launcher at `~/.local/bin/claude-monitor`

Then restart Claude Code for the statusline, and run `claude-monitor` from anywhere for the TUI.

### Updating

Since the installer symlinks everything back to the repo, pulling is all you need:

```bash
cd ~/Projects/claude-monitor
git pull
```

No reinstall required.

## How it works

1. **Session discovery** — scans `~/.claude/projects/**/*.jsonl` for files modified in the last 24 hours
2. **Full file parse** — single-pass scan of each JSONL for tokens, MCP calls, tool activity, timestamps, custom titles
3. **Status detection** — checks `~/.claude/session-signals/` for hook-based status, falls back to timing heuristics (< 30s = working, < 5min = waiting, else idle)
4. **Index metadata** — reads `sessions-index.json` for AI-generated summaries and project names
5. **Subagent detection** — finds `{session_id}/subagents/*.jsonl` directories, counts `agent-acompact-*` files as compactions
6. **Activity generation** — maps last tool call to human-readable gerund ("Reading config.py", "Searching Gmail"), converts to past tense for idle sessions

### Data sources

| Data | Source |
|------|--------|
| Session list | `~/.claude/projects/**/*.jsonl` (by mtime) |
| Session name | `/rename` → `custom-title` event in JSONL |
| Session summary | `sessions-index.json` → `summary` field |
| Status | Signal files or timing heuristics |
| Context % | Last API call's input tokens vs 200k window |
| Tokens / cost | Accumulated from `usage` blocks in assistant messages |
| MCP calls | Count of `"mcp__"` occurrences in JSONL |
| Compactions | Count of `agent-acompact-*.jsonl` in subagents dir |
| Remote URL | `slug` field → `https://claude.ai/code/session_{slug}` |
| Activity | Last `tool_use` block + gerund mapping, or text pattern extraction |

### Optional: session state hooks

For more accurate status detection, add hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "echo working > ~/.claude/session-signals/$CLAUDE_SESSION_ID"}]}],
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo stop > ~/.claude/session-signals/$CLAUDE_SESSION_ID"}]}]
  }
}
```

## Configuration

Preferences are saved to `~/.claude/monitor-prefs.json`:

```json
{
  "columns": ["status", "session", "project", "model", "context", "compact", "tokens", "cost", "active", "doing"],
  "column_order": ["status", "session", "project", "model", "context", "compact", "tokens", "cost", "mcp", "msgs", "duration", "active", "doing"]
}
```

Use the column picker (`c`) to customize which columns are visible and their order.

### Available columns

| Column | Description | Default |
|--------|-------------|---------|
| Status | Working / Waiting / Idle with color | ✓ |
| Session | Name or AI summary | ✓ |
| Project | Project directory | ✓ |
| Model | Opus 4.6, Sonnet 4.6, etc. | ✓ |
| Context | Colored bar + percentage | ✓ |
| Compacts | ✻ markers for compaction count | ✓ |
| Tokens | Total token count | ✓ |
| Cost | Estimated $ spent | ✓ |
| MCP | MCP tool call count | |
| Msgs | Message count | |
| Duration | Session lifetime | |
| Active | Time since last activity | ✓ |
| Doing | Current activity / last action | ✓ |

## Testing

The test suite covers pure functions, rendering, transcript parsing, and full TUI interactions using Textual's headless test framework.

```bash
# Install dev dependencies
uv sync --group dev

# Run all tests
uv run pytest tests/ -v
```

### Test structure

| File | Tests | What it covers |
|------|-------|----------------|
| `test_formatting.py` | 40 | format_model, format_tokens, format_cost, context bar, compactions |
| `test_gerunds.py` | 25 | Activity generation — gerund mapping, MCP tools, text extraction, past tense |
| `test_parsing.py` | 25 | Transcript parsing, timestamp handling, status detection, sorting |
| `test_rendering.py` | 20 | Row rendering, column config, truncation, subagent display |
| `test_tui.py` | 28 | Full app: keybindings, session menu, column picker, search, subagents |

## Contributing

The repo uses branch protection — all changes go through PRs.

```bash
# Fork or clone
git clone https://github.com/maxtomachine/claude-monitor.git
cd claude-monitor

# Create a branch
git checkout -b my-feature

# Make changes, run tests
uv run pytest tests/ -v

# Push and open a PR
git push -u origin my-feature
gh pr create
```

When adding new features, add corresponding tests. The TUI tests use Textual's `run_test()` pilot to simulate the full app headlessly — no terminal required.

## Roadmap

- [ ] macOS menu bar companion — session count + status in the menu bar
- [ ] Notification on session completion
- [ ] Historical cost tracking across sessions
- [ ] GitHub Actions CI for tests on PR

---

# Companion: Custom Statusline

A two-line statusline script for Claude Code that shows session context at a glance.

```
my-session | https://claude.ai/code/session_abc123
▓▓▓▓▓▓░░ 74% | ●●●●○ 80% | 89k tok | $2.05 | Opus 4.6
```

### Statusline features

- **Line 1**: Session name (via `/rename`) or cwd, plus remote control link
- **Line 2**: Context remaining bar, usage quota bar, total tokens, cost, model
- **Color rules**: context turns yellow below 50%, red with ⚠ below 25%
- **Fail-safe**: subshell rendering with fallback, log rotation at `/tmp/claude-statusline.log`

### Statusline install

Handled automatically by `./install.sh`. To install manually, place the script at `~/.claude/statusline.sh` and add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/statusline.sh"
  }
}
```

### Statusline JSON input reference

The script receives JSON on stdin:

| Field | Description |
|-------|-------------|
| `session_id` | Current session UUID |
| `session_name` | Custom session name (if set via `/rename`) |
| `transcript_path` | Path to session JSONL |
| `cwd` | Working directory |
| `model.id` / `model.display_name` | Model info (object, not string) |
| `cost.total_cost_usd` | Running cost |
| `context_window.remaining_percentage` | Context % left |
| `context_window.total_input_tokens` | Cumulative input tokens |
| `context_window.total_output_tokens` | Cumulative output tokens |
| `context_window.context_window_size` | Window size (e.g. 200000) |

---

Built with [Textual](https://textual.textualize.io/) and [Rich](https://rich.readthedocs.io/).
