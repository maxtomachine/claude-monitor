#!/usr/bin/env bash
# One-shot installer for claude-monitor + statusline on a new machine.
# Run from the repo root: ./install.sh
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"

echo "Installing claude-monitor from $REPO_DIR"

# ── Prerequisites ──────────────────────────────────────────────────────────────

if ! command -v uv &>/dev/null; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v jq &>/dev/null; then
  echo "Installing jq..."
  if command -v brew &>/dev/null; then
    brew install jq
  else
    echo "ERROR: jq is required. Install it manually: https://jqlang.github.io/jq/download/"
    exit 1
  fi
fi

# ── Python environment ─────────────────────────────────────────────────────────

echo "Setting up Python environment..."
cd "$REPO_DIR"
uv sync

# ── macOS space-switching ──────────────────────────────────────────────────────

# Jump-to-terminal needs this to switch desktops when activating an app
if ! defaults read com.apple.dock workspaces-auto-swoosh &>/dev/null; then
  defaults write com.apple.dock workspaces-auto-swoosh -bool YES
  killall Dock 2>/dev/null
  echo "Enabled space-switching on app activate"
fi

# ── Statusline ─────────────────────────────────────────────────────────────────

mkdir -p "$CLAUDE_DIR"
ln -sf "$REPO_DIR/statusline/statusline.sh" "$CLAUDE_DIR/statusline.sh"
echo "Linked statusline → $CLAUDE_DIR/statusline.sh"

# Add statusLine config to settings.json if not already present
SETTINGS="$CLAUDE_DIR/settings.json"
if [ -f "$SETTINGS" ]; then
  if ! grep -q '"statusLine"' "$SETTINGS"; then
    tmp=$(mktemp)
    jq '. + {"statusLine": {"type": "command", "command": "bash ~/.claude/statusline.sh"}}' "$SETTINGS" > "$tmp" && mv "$tmp" "$SETTINGS"
    echo "Added statusLine to $SETTINGS"
  else
    echo "statusLine already configured in $SETTINGS"
  fi
else
  echo '{"statusLine": {"type": "command", "command": "bash ~/.claude/statusline.sh"}}' > "$SETTINGS"
  echo "Created $SETTINGS with statusLine config"
fi

# ── Session tracker hook ───────────────────────────────────────────────────────

HOOKS_DIR="$CLAUDE_DIR/hooks"
mkdir -p "$HOOKS_DIR"
cp "$REPO_DIR/hooks/session_tracker.py" "$HOOKS_DIR/session_tracker.py"
echo "Installed hook → $HOOKS_DIR/session_tracker.py"

# Add hooks config if not already present
if [ -f "$SETTINGS" ] && ! grep -q '"SessionStart"' "$SETTINGS"; then
  tmp=$(mktemp)
  jq '. + {"hooks": {
    "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/session_tracker.py session_start"}]}],
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/session_tracker.py user_prompt_submit"}]}],
    "PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/session_tracker.py pre_tool_use"}]}],
    "PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/session_tracker.py post_tool_use"}]}],
    "PermissionRequest": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/session_tracker.py permission_request"}]}],
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/session_tracker.py stop"}]}],
    "SessionEnd": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/session_tracker.py session_end"}]}]
  }}' "$SETTINGS" > "$tmp" && mv "$tmp" "$SETTINGS"
  echo "Added hooks config to $SETTINGS"
else
  echo "Hooks already configured in $SETTINGS"
fi

# ── Monitor preferences ────────────────────────────────────────────────────────

if [ ! -f "$CLAUDE_DIR/monitor-prefs.json" ]; then
  cp "$REPO_DIR/monitor-prefs.json" "$CLAUDE_DIR/monitor-prefs.json"
  echo "Copied monitor-prefs.json → $CLAUDE_DIR/"
else
  echo "monitor-prefs.json already exists, skipping"
fi

# ── Launcher script ────────────────────────────────────────────────────────────

LAUNCHER="$HOME/.local/bin/claude-monitor"
mkdir -p "$(dirname "$LAUNCHER")"
cat > "$LAUNCHER" << EOF
#!/usr/bin/env bash
cd "$REPO_DIR" && uv run python claude_monitor.py "\$@"
EOF
chmod +x "$LAUNCHER"
echo "Created launcher → $LAUNCHER"

# Check PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
  echo ""
  echo "NOTE: Add ~/.local/bin to your PATH if not already there:"
  echo '  export PATH="$HOME/.local/bin:$PATH"'
fi

echo ""
echo "Done! Restart Claude Code for the statusline. Run 'claude-monitor' for the TUI."
