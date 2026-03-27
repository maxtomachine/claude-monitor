# Claude Monitor

See what all your Claude Code sessions are doing at a glance.

![Python](https://img.shields.io/badge/python-3.12+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![macOS](https://img.shields.io/badge/macOS-supported-brightgreen)

## The problem

You've got 6 Claude Code sessions open across 3 terminal windows. One is thinking, two are waiting for approval, one finished 20 minutes ago, and you can't remember which tab has which. You're alt-tabbing between windows to check on each one.

## The solution

A terminal dashboard that shows every session's status, what it's working on, how much context it's burned, and what it costs — updated in real time. Plus a two-line HUD statusline inside each Claude session.

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ Status   Session                    Model      Context     Tokens   Cost   Doing    │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ ✻ WORKING Build monitor dashboard   Opus 4.6   ██████████  245k    $3.42  Editing…  │
│ ? APPROVE Delete empty Gmail drafts  Opus 4.6   ████░░░░░░  1.2M    $6.76  Bash…    │
│ ◌ IDLE    Refactor auth middleware   Sonnet 4.6 ██░░░░░░░░  890k   $12.50  Edited…  │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

Working sessions animate with a `·*✢✳✶✻` spinner. Press `k` for a kanban board:

```
┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│ ⊘ Closed │  │ ◌ Idle   │  │ ○ Waiting │  │ ◉ Approve│  │ ● Working│
│   (2)    │  │   (1)    │  │   (0)    │  │   (1)    │  │   (2)    │
├──────────┤  ├──────────┤  │          │  ├──────────┤  ├──────────┤
│ auth-    │  │ refactor-│  │    —     │  │ gmail-   │  │ ✻ monitor│
│ work     │  │ module   │  │          │  │ cleanup  │  │ Editing… │
│          │  │ Edited…  │  │          │  │          │  ├──────────┤
│ old-     │  │          │  │          │  │          │  │ ✶ deploy │
│ session  │  │          │  │          │  │          │  │ Running… │
└──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘
```

## Install

Requires macOS, Python 3.12+, and at least one of: Ghostty, iTerm2, or Terminal.app.

```bash
git clone https://github.com/maxtomachine/claude-monitor.git
cd claude-monitor
./install.sh
```

The installer sets up everything:
- Python environment via [uv](https://docs.astral.sh/uv/)
- Statusline HUD symlinked into Claude Code
- Session tracker hooks for real-time status
- `claude-monitor` command on your PATH

**Restart Claude Code** after install to pick up the statusline and hooks. Then run:

```bash
claude-monitor
```

### Updating

```bash
cd claude-monitor && git pull
```

The statusline is symlinked, so pulling updates it everywhere instantly.

## What you get

### 1. The Dashboard (TUI)

A Textual-based terminal app showing all active Claude sessions in a sortable, searchable table.

| Key | What it does |
|-----|--------------|
| `Enter` | Action menu — jump to terminal, rename, copy ID, open remote, kill |
| `k` | Kanban board — sessions grouped by status column, arrow-key navigation |
| `s` | Cycle sort — activity, status, context %, tokens, cost |
| `/` | Search — filter by session name, project, model, or status |
| `c` | Column picker — show/hide columns, reorder with Shift+arrows |
| `a` | Toggle subagent rows — see spawned agents nested under parents |
| `z` | Show archived — include closed/old sessions with option to resume |
| `t` | Dark/light theme toggle |
| `n` | Send `/rename` to the selected session (patches unnamed sessions) |
| `R` | Restart the monitor in-place (picks up code changes) |
| `r` | Force refresh |
| `q` | Quit |

**Session actions** (Enter on any session):
- **Jump to terminal** — raises the right Ghostty/iTerm2/Terminal window, even across tabs (~200ms)
- **Resume** — reattach to a closed session in a new terminal tab
- **Send /rename** — tell Claude to auto-generate a session name
- **Copy session ID** — for `--resume` or debugging
- **Open remote control** — `claude.ai/code/session_*` link
- **Open transcript** — reveal the JSONL in Finder
- **Debrief & close** — run `/debrief` then close the tab
- **Kill process** — SIGTERM the Claude process

If jump-to-terminal can't find the window (renamed tab, moved to a different space), it falls back to opening a new tab and resuming the session there.

### 2. The Statusline (HUD)

A two-line display inside every Claude Code session:

```
ctx ██████░░▒▒  58%  🧠 max    341k tok
·636519dc  ⚡ fast  Bash        42   $
```

**Line 1:** Context bar (color-coded: green → yellow → red → blinking at 90%+), effort level, token count.

**Line 2:** Session ID marker (enables jump-to-terminal), fast mode indicator, current tool, session cost.

The statusline reads effort and fast-mode state from `~/.claude/settings.json`, so indicators show correctly from the moment a session starts — no need to run `/effort` or `/fast` first.

### 3. The Hooks

Seven Claude Code hooks fire on session events and write state to `~/.claude/session-states/`:

| Hook | What it tracks |
|------|---------------|
| SessionStart | New session → idle state |
| UserPromptSubmit | User sent a prompt → thinking |
| PreToolUse / PostToolUse | Tool running → tool name + target file/command |
| PermissionRequest | Waiting for approval |
| Stop | Claude finished responding → idle |
| SessionEnd | Session closed → exited (terminal state) |

The hooks also:
- **Set terminal titles** with a unique `·{sid8}` marker via `/dev/tty` writes — this is how jump-to-terminal finds the right window
- **Generate session titles** via `session-memory/summary.md` or a background Haiku API call (needs an API key at `~/.claude-monitor/.api_key`)
- **Guard state transitions** — exited is terminal (no flip-back), subagent events don't overwrite parent state

## How it works

### Status detection (3-tier fallback)

1. **Hook state files** — instant, event-driven, always accurate when hooks are installed
2. **PID checking** — process alive/dead via `~/.claude/sessions/*.json`
3. **Timing heuristics** — < 30s = working, < 5min = waiting, else idle (for sessions started before hooks)

### Performance

- **Warm refresh: ~11ms** — hook state provides status/tool, stale transcript cache reused for slow-changing data (tokens, model, cost)
- **Cold refresh: ~180ms** — full JSONL transcript scan, cached by mtime
- **Jump-to-terminal: ~200ms** — single JXA call with bulk title scan + `byName()` lookup

### Terminal support

| Terminal | Jump | Resume | Tested |
|----------|------|--------|--------|
| Ghostty | Yes | Yes (Cmd+T) | Primary |
| iTerm2 | Yes | Yes (Cmd+T) | Supported |
| Terminal.app | Yes | Yes (do script) | Fallback |

### Jumpback (Ctrl+Shift+Space)

Press `Ctrl+Shift+Space` from anywhere to raise the monitor window. Uses
[skhd](https://github.com/koekeishiya/skhd) — `install.sh` sets this up
automatically if you have Homebrew.

**First-time setup:** macOS will prompt for Accessibility permission when
you first press the keybind. Grant it at System Settings → Privacy &
Security → Accessibility → enable `skhd`. One-time per machine.

The script is at `~/.local/bin/jumpback` if you want to bind it
differently (Shortcuts.app, Karabiner, etc.).

## Configuration

Preferences persist at `~/.claude/monitor-prefs.json`:

```json
{
  "columns": ["status", "session", "project", "model", "context", "tokens", "cost", "active", "doing"],
  "theme": "textual-dark"
}
```

### Available columns

| Column | What it shows | Default |
|--------|---------------|---------|
| Status | Animated status with spinner | on |
| Session | Name or AI-generated summary | on |
| Project | Working directory | on |
| Model | Opus 4.6, Sonnet 4.6, etc. | on |
| Context | Color bar + percentage | on |
| Compacts | Context compaction count | on |
| Tokens | Total input + output tokens | on |
| Cost | Estimated USD spent | on |
| MCP | MCP tool call count | off |
| Msgs | Human + assistant message count | off |
| Duration | Session lifetime | off |
| Active | Time since last activity | on |
| Doing | Current activity description | on |

## Testing

```bash
uv sync --group dev
uv run pytest tests/ -v
```

172 tests across 6 files: formatting, gerund generation, transcript parsing, row rendering, full TUI integration (Textual pilot), and tmux-based end-to-end tests.

## Requirements

- **macOS** (uses JXA/System Events for terminal management)
- **Python 3.12+**
- **uv** (installed automatically by `install.sh`)
- **jq** (installed automatically on macOS via Homebrew)
- **Claude Code** (what you're monitoring)

## Contributing

```bash
git checkout -b my-feature
uv run pytest tests/ -v   # run before committing
git push -u origin my-feature
gh pr create
```

---

Built with [Textual](https://textual.textualize.io/) and [Rich](https://rich.readthedocs.io/).
