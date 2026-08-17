# Claude Monitor

See what all your Claude Code sessions are doing at a glance.

![Python](https://img.shields.io/badge/python-3.12+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![macOS](https://img.shields.io/badge/macOS-supported-brightgreen)

## The problem

You've got 6 Claude Code sessions open across 3 terminal windows. One is thinking, two are waiting for approval, one finished 20 minutes ago, and you can't remember which tab has which. You're alt-tabbing between windows to check on each one.

## The solution

A terminal dashboard that shows every session's name, whether it's working, blocked on you, or just done, and what it's actually doing — updated in real time. Plus a two-line HUD statusline inside each Claude session.

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ Session                            Status         Doing                             │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ Build monitor dashboard            ● WORKING       Editing claude_monitor.py         │
│ Delete empty Gmail drafts          ◉ APPROVE       Awaiting approval                 │
│ Refactor auth middleware           ○ done 22m      Edited auth.py                    │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

Working sessions animate with a `·*✢✳✶✻` spinner. Status is deliberately just three states: working, needs your approval, or done N-ago — the finer-grained guesses (idle vs waiting vs background) turned out to be reconstructed from decaying timers and didn't mean anything you'd act on, so they're gone. Context %, tokens, cost, and the rest are still there as optional columns (`Ctrl+C`), off by default now that context is Claude's own job to manage.

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
- `Ctrl+Shift+Space` global hotkey to jump back to the monitor (via skhd)

**Then three steps to go:**

1. **Restart Claude Code** — picks up the statusline and hooks
2. **Run `claude-monitor`** — opens the dashboard
3. **Grant Accessibility when prompted** — first `Ctrl+Shift+Space` press
   will ask; click "Open System Settings" → enable skhd. One-time.

That's it. The monitor will remind you about the hotkey for your first
20 launches.

### Updating

```bash
cd claude-monitor && git pull
```

The statusline is symlinked, so pulling updates it everywhere instantly.

## What you get

### 1. The Dashboard (TUI)

A Textual-based terminal app showing all active Claude sessions in a sortable, searchable table.

Plain letters, typed in sequence, are a Finder-style type-ahead: they jump the cursor to the group whose name starts with what you typed (e.g. `s` `t` `r` `a` → strategy). Every command below sits on `Ctrl+letter` instead, so it never fires mid-word. `K`/`R`/`P` were already Shift-bound and don't collide.

| Key | What it does |
|-----|--------------|
| `Enter` | Action menu — jump to terminal, rename, copy ID, open remote, kill |
| `letter` (typed in sequence) | Type-ahead jump to a group by name |
| `Ctrl+s` | Cycle sort — activity, status, context %, tokens, cost |
| `/` | Search — filter by session name, project, model, or status |
| `Ctrl+c` | Column picker — show/hide columns, reorder with Shift+arrows |
| `Ctrl+a` | Toggle subagent rows — see spawned agents nested under parents |
| `Ctrl+z` | Show archived — include closed/old sessions with option to resume |
| `Ctrl+n` | Send `/rename` to the selected session (patches unnamed sessions) |
| `Ctrl+p` | Pin/unpin: a pin never expires on its own |
| `Ctrl+o` | Hide/show pinned-but-inactive sessions in the default view (doesn't touch the pin itself) |
| `P` | Broadcast `/proactive` to all sessions in the cursor's group |
| `double-click` | Jump straight to the session's terminal (single-click highlights) |
| `Shift+Up/Down`, `Shift+Click` | Extend multi-row selection |
| `Delete` | Hide selected closed/archived row(s); press twice to confirm |
| `R` | Restart the monitor in-place (picks up code changes) |
| `Ctrl+r` | Force refresh |
| `Ctrl+q` | Quit |

**Row markers**: `⊙` pinned · `↻` scheduled run (sdk/headless; collapsed to the latest per project by default) · a pulsing `●` means the session just flipped to needing your approval (clears when you jump to it, fades after 5 min).

**Opting a session out**: name a session with `&ignore` anywhere in it (e.g. `/rename scratch&ignore`) and the monitor stops tracking it entirely: every view, subagents included, even if pinned. Remove the marker to track it again.

**Session actions** (Enter on any session):
- **Jump to terminal** — raises the right Ghostty/iTerm2/Terminal window, even across tabs (~200ms)
- **Resume** — reattach to a closed session in a new terminal tab
- **Send /rename** — tell Claude to auto-generate a session name
- **Copy session ID** — for `--resume` or debugging
- **Open remote control** — `claude.ai/code/session_*` link
- **Open transcript** — reveal the JSONL in Finder
- **Debrief & close** — run `/debrief` then close the tab
- **Kill process** — SIGTERM the Claude process

If jump-to-terminal can't find the window (renamed tab, moved to a different space), it falls back to opening a new Ghostty window and resuming the session there, launched through Ghostty's own scripting rather than a simulated keystroke (see CLAUDE.md).

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

Want the exact Ghostty setup the screenshots use (JetBrains Mono, custom Gruvbox light/dark that follows macOS appearance, prompt-jump keybinds)? Copy [`extras/ghostty/`](extras/ghostty/) into `~/.config/ghostty/` and restart Ghostty.

### Jumpback (Ctrl+Shift+Space)

Press `Ctrl+Shift+Space` from anywhere to raise the monitor window. Uses
[skhd](https://github.com/koekeishiya/skhd) — `install.sh` sets this up
automatically if you have Homebrew.

**First-time setup:** macOS will prompt for Accessibility permission when
you first press the keybind. Grant it at System Settings → Privacy &
Security → Accessibility → enable `skhd`. One-time per machine.

The script is at `~/.local/bin/jumpback` if you want to bind it
differently (Shortcuts.app, Karabiner, etc.).

### Jump to next (Ctrl+Shift+N, and "n" inside the monitor)

Press `Ctrl+Shift+N` from anywhere to instantly jump to the next session
that needs you (blocked on your approval, or finished and waiting on your
next move), without opening the monitor at all — `claude-monitor
--jump-next` runs headless: it parses sessions, picks the target, jumps or
resumes, and exits. Press repeatedly to walk through everything that needs
you, oldest-waiting first, needs-approval before done. A READY session you
land on this way is marked seen: its status stays bold and still says
READY, but the color moves off yellow to mint, and its row title unbolds,
until it cycles through another state and becomes READY again.

Inside the monitor, plain `n` (no modifier) only moves the cursor to the
next session that needs you; it does not jump. `n` then `Enter` then
`Enter` (move, open the menu, pick Jump) does jump — naked `n` alone
doesn't. `n` is a deliberate exception to "every hotkey needs Ctrl"
(alongside `K`/`R`/`P`): it's meant to be a single unmodified keystroke, at
the cost of `n` no longer being available for type-ahead group jump.

Add the `skhd` binding yourself (`install.sh` doesn't do this one
automatically):

```
ctrl + shift - n : ~/.local/bin/claude-monitor --jump-next
```

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
| Session | Name or AI-generated summary | on |
| Status | working / needs approval / done N-ago, with spinner | on |
| Doing | Last real tool/activity, fact-derived from the transcript | on |
| Duration | Session lifetime | off |
| Project | Working directory | off |
| Model | Opus 4.6, Sonnet 4.6, etc. | off |
| Context | Color bar + percentage | off |
| Compacts | Context compaction count | off |
| Tokens | Total input + output tokens | off |
| Cost | Estimated USD spent | off |
| MCP | MCP tool call count | off |
| Msgs | Human + assistant message count | off |
| Active | Time since last activity | off |
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
