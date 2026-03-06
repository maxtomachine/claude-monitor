#!/usr/bin/env bash
# Install claude-monitor statusline into Claude Code
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$HOME/.claude/statusline.sh"

# Symlink the statusline script
ln -sf "$SCRIPT_DIR/statusline.sh" "$TARGET"
echo "Linked $TARGET -> $SCRIPT_DIR/statusline.sh"

# Add statusLine config to settings.json if not already present
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
  if ! grep -q '"statusLine"' "$SETTINGS"; then
    tmp=$(mktemp)
    jq '. + {"statusLine": {"type": "command", "command": "bash ~/.claude/statusline.sh"}}' "$SETTINGS" > "$tmp" && mv "$tmp" "$SETTINGS"
    echo "Added statusLine config to $SETTINGS"
  else
    echo "statusLine already configured in $SETTINGS"
  fi
else
  echo '{"statusLine": {"type": "command", "command": "bash ~/.claude/statusline.sh"}}' > "$SETTINGS"
  echo "Created $SETTINGS with statusLine config"
fi

echo "Done. Restart Claude Code to see the statusline."