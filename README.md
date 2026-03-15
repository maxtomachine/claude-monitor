# Claude Monitor

A btop-style terminal dashboard for monitoring Claude Code sessions in real-time, plus a custom two-line HUD statusline that lives inside every Claude Code session.

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
- **Context bar** — colored progress bar (green → yellow → red as context depletes)
- **Compaction tracking** — colored ✻ markers show how many times context was compacted
- **Cost estimation** — per-session cost based on model pricing and token counts
- **MCP tool call counts** — see which sessions are heavy MCP users
- **Subagent tree view** — expand sessions to see their subagents indented below
- **Detail panel** — shows last assistant message preview for the highlighted session
- **Session actions** — Enter on any session for: jump to terminal, copy ID, open remote control, reveal transcript
- **Multi-terminal support** — jump to terminal works across Ghostty, iTerm2, Terminal, Kitty, Alacritty, WezTerm, and Warp
- **Sort modes** — cycle through: last active, status, context %, tokens, cost
- **Search/filter** — `/` to filter sessions by name, project, status, or model
- **Column picker** — toggle columns on/off, reorder with Shift+arrows, preferences saved to disk
- **Statusline config** — configure the in-session HUD statusline with a live preview
- **Pause all** — send SIGINT to all working Claude Code processes
- **Remote control URLs** — open `claude.ai/code/session_*` links directly

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `j` / `k` / arrows | Navigate sessions |
| `Enter` | Open action menu for selected session |
| `s` | Cycle sort mode |
| `t` | Toggle subagent tree view |
| `c` | Open column picker |
| `l` | Open statusline config with live preview |
| `p` | Pause all working sessions |
| `/` | Search / filter |
| `Esc` | Clear search |
| `r` | Manual refresh |
| `q` | Quit |

---

## The Statusline

A two-line HUD that lives inside every Claude Code session, showing context usage, rate limits, effort/speed modes, and session costs at a glance.

```
ctx ██░░░░░░▒▒  22%  🧠 high   113k tok
use ▮▮▮▮▮▮▮▮▮▮   8%  ⚡ fast    24   $
```

### What each part shows

**Line 1 — Context window:**
- 10-block colored bar (green → yellow → red → blink red at 90%+)
- Effort level indicator (🧠) — only when `/effort` is set to non-auto
- Total token count

**Line 2 — Usage quota:**
- 10-block blue gradient ammo bar showing 5-hour rate limit usage
- Fast mode indicator (⚡) — only when `/fast` is active
- Session cost (rounded integer)

### Design principles

- **Most important info is leftmost** — bars and indicators survive even the narrowest terminal widths
- **Columns align vertically** — tokens over cost, effort emoji over fast emoji, all lined up
- **No truncation** — the terminal clips naturally; no complex width detection needed
- **Indicators only when active** — 🧠 and ⚡ hide when effort is "auto" and fast is off, keeping the HUD clean
- **Configurable** — press `l` in the monitor TUI to toggle parts on/off with a live preview

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

---

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
| Context % | Ground-truth from statusline or token estimation |
| Tokens / cost | Accumulated from `usage` blocks in assistant messages |
| MCP calls | Count of `"mcp__"` occurrences in JSONL |
| Compactions | Count of `agent-acompact-*.jsonl` in subagents dir |
| Remote URL | `slug` field → `https://claude.ai/code/session_{slug}` |
| Activity | Last `tool_use` block + gerund mapping, or text pattern extraction |
| Effort level | Parsed from transcript (`/effort` command output) |
| Fast mode | `output_style.name` from statusline JSON |
| Usage quota | Anthropic API (`/api/oauth/usage`), cached 60s |

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

---

## Configuration

Preferences are saved to `~/.claude/monitor-prefs.json`:

```json
{
  "columns": ["status", "session", "project", "model", "context", "compact", "tokens", "cost", "active", "doing"],
  "column_order": ["status", "session", "project", "model", "context", "compact", "tokens", "cost", "mcp", "msgs", "duration", "active", "doing"],
  "statusline": {
    "quota_bar": true,
    "fast_mode": true
  }
}
```

### Column picker (`c`)

Toggle columns on/off and reorder them with Shift+arrows. Preferences persist across sessions.

| Column | Description | Default |
|--------|-------------|---------|
| Status | Working / Waiting / Idle with color | on |
| Session | Name or AI summary | on |
| Project | Project directory | on |
| Model | Opus 4.6, Sonnet 4.6, etc. | on |
| Context | Colored bar + percentage | on |
| Compacts | ✻ markers for compaction count | on |
| Tokens | Total token count | on |
| Cost | Estimated $ spent | on |
| MCP | MCP tool call count | off |
| Msgs | Message count | off |
| Duration | Session lifetime | off |
| Active | Time since last activity | on |
| Doing | Current activity / last action | on |

### Statusline config (`l`)

Toggle statusline parts with a live mock preview. Currently active toggles:

| Part | Description | Default |
|------|-------------|---------|
| Quota bar | Usage quota ammo bar on line 2 | on |
| Fast mode | ⚡ indicator when /fast is active | on |

---

## Testing

150 tests covering pure functions, rendering, transcript parsing, and full TUI interactions.

```bash
uv sync --group dev
uv run pytest tests/ -v
```

| File | Tests | What it covers |
|------|-------|----------------|
| `test_formatting.py` | 40 | format_model, format_tokens, format_cost, context bar, compactions |
| `test_gerunds.py` | 25 | Activity generation — gerund mapping, MCP tools, text extraction, past tense |
| `test_parsing.py` | 25 | Transcript parsing, timestamp handling, status detection, sorting |
| `test_rendering.py` | 20 | Row rendering, column config, truncation, subagent display |
| `test_tui.py` | 28 | Full app: keybindings, session menu, column picker, search, subagents |

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

When adding new features, add corresponding tests. The TUI tests use Textual's `run_test()` pilot to simulate the full app headlessly — no terminal required.

---

## Roadmap

- [ ] Convert hot-path parsing to compiled language (Rust/Go) for performance
- [ ] macOS menu bar companion — session count + status in the menu bar
- [ ] Notification on session completion
- [ ] Historical cost tracking across sessions
- [ ] GitHub Actions CI for tests on PR

---

Built with [Textual](https://textual.textualize.io/) and [Rich](https://rich.readthedocs.io/).
