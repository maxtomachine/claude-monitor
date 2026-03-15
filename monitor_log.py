#!/usr/bin/env python3
"""Structured logging for Claude Monitor.

Writes JSON-lines to ~/.claude/monitor.log with automatic rotation.
Can be tailed with `uv run python monitor_log.py` for a live formatted view.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path.home() / ".claude" / "monitor.log"
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB — rotate when exceeded
KEEP_ROTATIONS = 2

enabled = True  # Toggle via 'd' hotkey in monitor


def _rotate_if_needed() -> None:
    """Rotate log file if it exceeds MAX_LOG_SIZE."""
    try:
        if not LOG_PATH.exists() or LOG_PATH.stat().st_size < MAX_LOG_SIZE:
            return
    except OSError:
        return

    # Shift old rotations
    for i in range(KEEP_ROTATIONS - 1, 0, -1):
        src = LOG_PATH.with_suffix(f".log.{i}")
        dst = LOG_PATH.with_suffix(f".log.{i + 1}")
        if src.exists():
            try:
                src.rename(dst)
            except OSError:
                pass

    # Current → .1
    try:
        LOG_PATH.rename(LOG_PATH.with_suffix(".log.1"))
    except OSError:
        pass


def log(category: str, event: str, **data) -> None:
    """Write a structured log entry.

    Args:
        category: Log category (e.g. "jump", "refresh", "debrief", "status")
        event: Short event name (e.g. "title_match", "pid_not_found", "signal_detected")
        **data: Arbitrary key-value pairs for context
    """
    if not enabled or "pytest" in sys.modules:
        return
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "cat": category,
        "event": event,
        **data,
    }
    _rotate_if_needed()
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass


# ── Live tail viewer ─────────────────────────────────────────────────────────

CAT_COLORS = {
    "jump": "cyan",
    "refresh": "dim",
    "debrief": "magenta",
    "status": "green",
    "close": "yellow",
    "error": "red",
    "signal": "blue",
}


def tail_log(path: Path = LOG_PATH, follow: bool = True, category: str | None = None) -> None:
    """Print formatted log entries, optionally following for new lines."""
    from rich.console import Console
    from rich.text import Text

    console = Console()

    def format_entry(line: str) -> Text | None:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None

        cat = entry.get("cat", "?")
        if category and cat != category:
            return None

        ts = entry.get("ts", "?")
        # Show just HH:MM:SS.mmm
        if "T" in ts:
            ts = ts.split("T")[1].rstrip("Z+")[:12]

        event = entry.get("event", "?")
        color = CAT_COLORS.get(cat, "white")

        # Build detail string from remaining keys
        skip = {"ts", "cat", "event"}
        details = {k: v for k, v in entry.items() if k not in skip}
        detail_str = "  ".join(f"{k}={v}" for k, v in details.items())

        text = Text()
        text.append(f"{ts} ", style="dim")
        text.append(f"[{cat}]", style=f"bold {color}")
        text.append(f" {event}", style="white")
        if detail_str:
            text.append(f"  {detail_str}", style="dim")
        return text

    if not path.exists():
        console.print(f"[dim]No log file at {path} — waiting for entries...[/]")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    # Print existing entries
    with path.open() as f:
        for line in f:
            text = format_entry(line.strip())
            if text:
                console.print(text)

    if not follow:
        return

    console.print("[dim]─── following ───[/]")

    # Tail follow
    with path.open() as f:
        f.seek(0, 2)  # EOF
        while True:
            line = f.readline()
            if line:
                text = format_entry(line.strip())
                if text:
                    console.print(text)
            else:
                time.sleep(0.3)


if __name__ == "__main__":
    cat_filter = None
    if len(sys.argv) > 1:
        cat_filter = sys.argv[1]
    tail_log(category=cat_filter)
