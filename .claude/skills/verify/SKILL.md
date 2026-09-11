---
name: verify
description: Build, launch and drive claude-monitor to observe a change actually working — including how to reach the TUI when the sandbox blocks ptys.
---

# Verifying claude-monitor

The surface is the TUI. Drive it; don't re-run pytest and call that
verification.

## Running anything

`uv run` panics inside Claude Code's Bash sandbox (`system-configuration`
crate, "Attempted to create a NULL object" — it reads the system network
proxy config). Call the venv interpreter directly:

```bash
.venv/bin/python -m pytest tests/ -q          # 491 pass, ~40s
.venv/bin/python claude_monitor.py            # needs a real terminal
```

`tests/test_tmux_e2e.py` errors with `error connecting to
/private/tmp/tmux-502/default` under the sandbox. That is not flake and not
your change: tmux needs a unix socket and the sandbox denies
`bind(AF_UNIX)` anywhere. Those 7 errors are expected; the other 491 are
the real signal.

## What the sandbox denies (check before concluding "broken")

| Blocked | Symptom | Consequence |
|---|---|---|
| `pty.openpty()` | `OSError: out of pty devices` | no real terminal, so no tmux/script capture |
| `bind(AF_UNIX)` | `PermissionError` on any `.sock` | tmux cannot start at all, any `-L`/`-S` |
| `ps` on other processes | empty output | `_pid_is_claude()` says no to everything |
| `os.kill(pid, 0)` on other processes | `OSError` | `_refresh_pid_map()` marks every session dead, so the PID-file orphan pass and the multi-PID sibling split never run |

The last two matter most: half of `parse_sessions()` is unreachable
unless you stub liveness. Stub it explicitly rather than concluding the
code does nothing.

## Driving the TUI headlessly

Textual's `run_test()` runs the real app — real `on_mount`, real refresh
worker, real render — with no terminal. Dump the composited screen with
`app.screen._compositor.render_strips()` (each `Strip` has `.text`);
`app.export_screenshot()` (SVG) is the fallback.

`/tmp/drive_monitor.py`-style harness, rebuilt as needed:

```python
spec = importlib.util.spec_from_file_location("cm", path)   # PYTHONPATH must
cm = importlib.util.module_from_spec(spec)                  # include the repo
spec.loader.exec_module(cm)                                 # (monitor_log)
cm._gc_state_files = lambda: None      # else it DELETES real hook state
cm._refresh_pid_map = lambda: None     # sandbox: os.kill is denied
cm._pid_map = {sid: pid for each ~/.claude/sessions/*.json}
cm._pid_is_claude = lambda pid: True
cm.os.kill = lambda *a, **k: None      # the sibling-split pass calls it inline

app = cm.ClaudeMonitor()
async with app.run_test(size=(120, 22)) as pilot:
    ...poll until app.sessions, then pilot.press(key)...
```

## Isolating from Max's live monitor (mandatory)

A real monitor is always running on this machine and its state files are
shared. Two environment variables cover it:

- `MONITOR_STATE_HOME=$(mktemp -d)` — redirects prefs, pins, hidden,
  layout and the scan cache. Real session data still reads from `~/.claude`.
  Copy `~/.claude/monitor-scan-cache.json` in first for a warm parse (0.8s
  vs ~5s).
- `HOME=/tmp/some-fake` — redirects *everything*, including `SESSIONS_DIR`,
  `HOOK_STATE_DIR` and `CLAUDE_DIR`. This is how you stage exact inputs.

Never let a probe write to the real `~/.claude`. `parse_sessions()` itself
writes only the scan cache, but `_gc_state_files()` unlinks hook state and
the App writes prefs on mount.

## The before/after pattern that earns a verdict

A phantom-row or title bug shows up as a row-set diff. Run the SAME staged
input through both builds:

```bash
git worktree add /tmp/cm-head HEAD        # pre-change build
HOME=/tmp/staged PYTHONPATH=/tmp/cm-head .venv/bin/python /tmp/drive.py /tmp/cm-head/claude_monitor.py
HOME=/tmp/staged PYTHONPATH=$PWD          .venv/bin/python /tmp/drive.py $PWD/claude_monitor.py
git worktree remove /tmp/cm-head
```

Staging a session is three files: `~/.claude/sessions/<pid>.json` (the PID
record: `pid`, `sessionId`, `cwd`, `kind`, `entrypoint`, `name`,
`nameSource`, `startedAt`, `updatedAt`, `status`), optionally
`~/.claude/session-states/<sid>.json` for a hook title, optionally a
transcript at `~/.claude/projects/<slug>/<sid>.jsonl`.

## Flows worth driving

- the table itself (does the row exist, what is its title, what is its status)
- `ctrl+b` inbox mode, `ctrl+z` history, `ctrl+c` columns, `/` search
- `space` opens the prompt box and NAMES ITS TARGET — a good check that a
  title change reached the send path, since that name is what the terminal
  matcher searches for
- `enter` opens SessionMenu

Jump, layout save/restore and anything else touching Accessibility or
Ghostty cannot run headless or sandboxed. Say so rather than guessing.
