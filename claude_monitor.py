#!/usr/bin/env python3
"""Claude Code session monitor — btop-style TUI."""

import http.server
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

def _escape_markup(text: str) -> str:
    """Escape all [ for Textual markup (rich.markup.escape misses some)."""
    return text.replace("[", "\\[")
from textual import events
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets.data_table import RowKey
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Header, Footer, Static, DataTable, Label, OptionList, Checkbox, Input,
)
from textual.widgets.option_list import Option
from textual.theme import Theme


# Gruvbox themes mirroring ~/.config/ghostty/themes/gruvbox-custom-{dark,light}
GRUVBOX_DARK = Theme(
    name="gruvbox-dark",
    dark=True,
    background="#282828",
    foreground="#b2ebbb",
    surface="#32302f",
    panel="#3c3836",
    boost="#504945",
    primary="#83a598",
    secondary="#d3869b",
    accent="#fabd2f",
    success="#b8bb26",
    warning="#d79921",
    error="#fb4934",
)
GRUVBOX_LIGHT = Theme(
    name="gruvbox-light",
    dark=False,
    background="#fbf1c7",
    foreground="#282828",
    surface="#f2e5bc",
    panel="#ebdbb2",
    boost="#d5c4a1",
    primary="#076678",
    secondary="#8f3f71",
    accent="#b57614",
    success="#79740e",
    warning="#b57614",
    error="#9d0006",
)


def _system_is_dark() -> bool:
    """macOS appearance — `defaults` exits 1 when light mode is active."""
    try:
        r = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=2,
        )
        return r.returncode == 0 and r.stdout.strip() == "Dark"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True


CLAUDE_DIR = Path.home() / ".claude" / "projects"
SIGNALS_DIR = Path.home() / ".claude" / "session-signals"
HOOK_STATE_DIR = Path.home() / ".claude" / "session-states"
TASKS_DIR = Path.home() / ".claude" / "tasks"
# Local HTTP listener for click-to-jump links (http://localhost:48624/jump/<sid8>)
JUMP_HTTP_PORT = 48624
# Dropped into the jump-request file by `claude-monitor --jump-next`
# (Ctrl+Shift+N) instead of a real sid/title, so an already-running monitor
# picks it up and jumps using its own warm session list.
JUMP_NEXT_SENTINEL = "__jump_next__"
# Dropped into the same request file by `claude-monitor --restart`, so an
# already-running monitor can be told to pick up code changes (the same
# thing Shift+R does) without a synthetic keystroke, which fires Claude
# Nest's push-to-talk regardless of which key is sent (see CLAUDE.md).
RESTART_SENTINEL = "__restart__"
# The one request file both directions of this protocol use: the running
# monitor's 200ms poller (_check_jump_request) reads it; --jump-next and
# --restart write it. One module-level constant (not three separate
# literals) so tests can monkeypatch a single name instead of three, and
# never touch this real shared path on a machine with a live monitor.
JUMP_REQUEST_PATH = Path("/tmp/claude-jump-request")
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
# Monitor-OWNED writable state (pins, hidden, prefs; log + instance records derive
# from the same home). Overridable via MONITOR_STATE_HOME so a staging instance
# (monitor2) keeps its own pins/log without touching production. Real session data
# (SESSIONS_DIR, HOOK_STATE_DIR, CLAUDE_DIR above) always reads from ~/.claude.
_STATE_HOME = Path(os.environ.get("MONITOR_STATE_HOME") or (Path.home() / ".claude"))
_STATE_HOME.mkdir(parents=True, exist_ok=True)
PREFS_PATH = _STATE_HOME / "monitor-prefs.json"
HIDDEN_PATH = _STATE_HOME / "monitor-hidden.json"
PINNED_PATH = _STATE_HOME / "monitor-pinned.json"
LAYOUT_PATH = _STATE_HOME / "monitor-layout.json"
SCAN_CACHE_PATH = _STATE_HOME / "monitor-scan-cache.json"
BELL_DECAY_S = 300


def _load_sid_set(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()


def _save_sid_set(path: Path, sids: set[str]) -> None:
    try:
        path.write_text(json.dumps(sorted(sids)))
    except OSError:
        pass


def load_hidden_sessions() -> set[str]:
    return _load_sid_set(HIDDEN_PATH)


def save_hidden_sessions(hidden: set[str]) -> None:
    _save_sid_set(HIDDEN_PATH, hidden)


def load_pinned_sessions() -> set[str]:
    return _load_sid_set(PINNED_PATH)


def save_pinned_sessions(pinned: set[str]) -> None:
    _save_sid_set(PINNED_PATH, pinned)
DOING_MAX_WIDTH = 40
RESTART_EXIT_CODE = 99
_update_available: str = ""  # Commit summary when remote is ahead
_REPO_DIR = Path(__file__).resolve().parent

MODEL_PRICING = {
    "claude-opus-4-6": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (0.80, 4.0),
}

MODEL_CONTEXT_WINDOW = {
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
}


def model_context_window(model_id: str, observed_tokens: int = 0) -> int:
    for k, w in MODEL_CONTEXT_WINDOW.items():
        if k in model_id:
            return max(w, _infer_window(observed_tokens))
    return _infer_window(observed_tokens)


def _infer_window(observed_tokens: int) -> int:
    # A prompt cannot exceed its window — so observed token count is a hard
    # lower bound. Snap up to the next standard size when the assumed window
    # is already exceeded (handles model IDs not in the map).
    for w in (200_000, 500_000, 1_000_000, 2_000_000):
        if observed_tokens < int(w * 0.95):
            return w
    return 2_000_000

# ── Gerund generation ─────────────────────────────────────────────────────────

MCP_SERVICE_NAMES = {
    "Google_Gmail_All_Access": "Gmail",
    "Google_Calendar_Edit": "Calendar",
    "Google_Drive": "Drive",
    "Google_Tasks": "Tasks",
    "Google_Contacts": "Contacts",
    "Monarch_Money": "Monarch",
    "Whoop_MCP": "WHOOP",
    "Cloudflare_Developer_Platform": "Cloudflare",
    "Plaud": "Plaud",
    "PDF_Viewer": "PDFs",
}

MCP_ACTION_GERUNDS = {
    "search": "Searching", "list": "Listing", "get": "Fetching",
    "create": "Creating", "delete": "Deleting", "edit": "Editing",
    "update": "Updating", "send": "Sending", "read": "Reading",
    "batch_modify": "Modifying", "reply": "Replying", "forward": "Forwarding",
    "trash": "Trashing", "move": "Moving", "copy": "Copying",
    "append": "Appending", "share": "Sharing", "rename": "Renaming",
    "restore": "Restoring", "complete": "Completing",
    "refresh": "Refreshing", "check": "Checking",
}

BASH_CMD_GERUNDS = {
    "git": "Running git", "npm": "Running npm", "pip": "Installing",
    "python": "Running Python", "python3": "Running Python", "node": "Running Node",
    "open": "Opening", "ls": "Listing files", "find": "Finding files",
    "curl": "Fetching URL", "mkdir": "Creating directory", "uv": "Running uv",
    "rm": "Removing files", "cp": "Copying files", "mv": "Moving files",
    "docker": "Running Docker", "make": "Building", "pnpm": "Running pnpm",
}

# Gerund → past tense for idle sessions
GERUND_TO_PAST = {
    "Reading": "Read", "Editing": "Edited", "Writing": "Wrote",
    "Running": "Ran", "Searching": "Searched", "Finding": "Found",
    "Fetching": "Fetched", "Loading": "Loaded", "Creating": "Created",
    "Deleting": "Deleted", "Updating": "Updated", "Sending": "Sent",
    "Modifying": "Modified", "Replying": "Replied", "Forwarding": "Forwarded",
    "Trashing": "Trashed", "Moving": "Moved", "Copying": "Copied",
    "Appending": "Appended", "Sharing": "Shared", "Renaming": "Renamed",
    "Restoring": "Restored", "Completing": "Completed", "Listing": "Listed",
    "Refreshing": "Refreshed", "Checking": "Checked", "Installing": "Installed",
    "Opening": "Opened", "Building": "Built", "Debugging": "Debugged",
    "Scanning": "Scanned", "Refactoring": "Refactored",
}

# Patterns for extracting gerunds from assistant text
TEXT_GERUND_PATTERNS = [
    # Already starts with a gerund
    (r'^([A-Z][a-z]+ing)\b(.{0,40})', None),
    # "Let me <verb>"
    (r'[Ll]et me (\w+)\s+(.{0,40})', 1),
    # "I'll/I will/I need to/I'm going to <verb>"
    (r"I(?:'ll| will| need to| want to|'m going to) (\w+)\s+(.{0,40})", 1),
    # "I'm <gerund>"
    (r"I'm (\w+ing)\s+(.{0,30})", None),
]

ALL_COLUMNS = {
    "session":   {"label": "Session",  "default": True},
    "status":    {"label": "Status",   "default": True},
    "doing":     {"label": "Doing",    "default": True},
    "duration":  {"label": "Duration", "default": False},
    "project":   {"label": "Project",  "default": False},
    "model":     {"label": "Model",    "default": False},
    "context":   {"label": "Context",  "default": False},
    "compact":   {"label": "Compacts", "default": False},
    "tokens":    {"label": "Tokens",   "default": False},
    "cost":      {"label": "Cost",     "default": False},
    "mcp":       {"label": "MCP",      "default": False},
    "msgs":      {"label": "Msgs",     "default": False},
    "active":    {"label": "Active",   "default": False},
}


class SortMode(Enum):
    ACTIVITY = "activity"
    STATUS = "status"
    ALPHA = "alpha"
    CONTEXT = "context"
    TOKENS = "tokens"
    COST = "cost"

    def next(self) -> "SortMode":
        members = list(SortMode)
        return members[(members.index(self) + 1) % len(members)]

    @property
    def label(self) -> str:
        return {
            SortMode.ACTIVITY: "Last Active", SortMode.STATUS: "Status",
            SortMode.ALPHA: "A–Z", SortMode.CONTEXT: "Context %",
            SortMode.TOKENS: "Tokens", SortMode.COST: "Cost",
        }[self]


STATUS_PRIORITY = {
    "needs_approval": 0, "working": 1, "debriefing": 2,
    "done": 3, "closed": 4, "archived": 5, "standby": 6,
}
STATUS_DISPLAY = {
    "needs_approval": ("◉ APPROVE", "yellow"),
    "working": ("● WORKING", "dim"),
    "debriefing": ("⏳ DEBRIEFING", "magenta"),
    "done": ("○ READY", "bright_yellow"),
    "closed": ("⊘ CLOSED", "rgb(100,100,100)"),
    "archived": ("◇ ARCHIVED", "dim"),
    "standby": ("◌ STANDBY", "dim"),
}
# Brightness here tracks "does this need you", not "is something happening":
# a working session is self-sufficient and will surface itself the moment it
# stops, so it doesn't earn the loud color; a done session is exactly the one
# sitting there waiting on you, so it does (Max, 2026-08-16: "I actually need
# to pay attention to the ones that are ready for me more than the ones that
# don't need me because they are already whirring"). needs_approval stays
# yellow, the one state that's actually blocking, above both.

# STATUS_DISPLAY["done"]'s bright_yellow is the dark-theme color only. In
# light mode (Gruvbox light's cream #fbf1c7 background), yellow is already
# the theme's own dominant tone, so it doesn't pop the way it does against
# a dark background (Max, 2026-08-17: "in light mode we need another color
# than yellow since that's our default"); a vivid blue reads clearly
# against cream instead. render_status_cell()/render_row() take a `dark`
# flag and swap to this; StatsBar.update_stats() reads self.app.theme to
# match. The mint "seen" color is unaffected: soft green reads fine on
# both backgrounds, this is only about the loud, unseen READY color.
READY_COLOR_DARK = "bright_yellow"
READY_COLOR_LIGHT = "#007AFF"

# The WORKING spinner glyph, a few steps off each theme's background so it
# is barely there: motion you can find if you look, never something the
# eye is pulled to. It was coral, then dim coral; a hue at any brightness
# still registered as a moving colored dot on every busy row (Max,
# 2026-08-18: "gray out the animating *, it's too distracting, dim it
# further to barely visible in both light and dark mode"). Gruvbox dark
# bg is #282828, light bg is #fbf1c7.
SPINNER_COLOR_DARK = "#4a4a4a"
SPINNER_COLOR_LIGHT = "#d5c4a1"

# READY read/unread (Max, 2026-08-16): jumping to a done session without
# sending it anything acknowledges it. Still bold, still says READY, the
# color just moves off yellow (or, in light mode, off blue) to mint so a
# glance tells you "already looked at this one" without it reading as
# closed or any less real.
READY_SEEN_COLOR = "#8FD9B6"

# The one test for "this row is inactive": rendered dim instead of bold
# (render_row), excluded from menus meant for live sessions, and what
# hide_inactive_pins hides. Getting this tuple wrong at just one of its
# call sites is exactly what caused two real bugs in the pin-hiding
# feature (2026-08-16): a single shared constant instead of the literal
# repeated at each site.
INACTIVE_STATUSES = ("archived", "closed")


@dataclass
class Session:
    session_id: str
    project: str
    title: str
    status: str
    model: str
    model_id: str
    cost: float
    tokens_in: int
    tokens_out: int
    context_pct: int
    message_count: int
    last_activity: float
    created: float
    cwd: str
    transcript_path: str
    remote_url: str = ""
    slug: str = ""
    is_subagent: bool = False
    is_scheduled: bool = False  # entrypoint != 'cli' (sdk-cli, headless, etc.)
    parent_id: str = ""
    subagents: list["Session"] = field(default_factory=list)
    compact_count: int = 0
    mcp_calls: int = 0
    last_tool: str = ""
    last_tool_input: dict = field(default_factory=dict)
    last_assistant_text: str = ""
    status_name: str = ""
    project_path: str = ""  # Original launch directory (for resume)
    background_count: int = 0
    context_is_estimate: bool = False
    sid: str = ""  # bare conversation id (for file lookups); session_id carries the row key
    instance_id: str = ""  # durable per-instance surrogate "{sid}#{startedAt_ms}"


# ── Preferences ───────────────────────────────────────────────────────────────


def load_prefs() -> dict:
    try:
        return json.loads(PREFS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_prefs(prefs: dict) -> None:
    try:
        PREFS_PATH.write_text(json.dumps(prefs, indent=2))
    except OSError:
        pass


def get_visible_columns() -> list[str]:
    prefs = load_prefs()
    saved = prefs.get("columns")
    if saved:
        return [c for c in saved if c in ALL_COLUMNS]
    return [k for k, v in ALL_COLUMNS.items() if v["default"]]


def get_column_order() -> list[str]:
    """Get the full column order (including hidden columns)."""
    prefs = load_prefs()
    saved = prefs.get("column_order")
    if saved:
        # Ensure all columns present (new ones appended at end)
        known = [c for c in saved if c in ALL_COLUMNS]
        for k in ALL_COLUMNS:
            if k not in known:
                known.append(k)
        return known
    return list(ALL_COLUMNS.keys())


# ── Data parsing ──────────────────────────────────────────────────────────────


def parse_timestamp(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0.0


_scan_cache: dict[str, tuple[float, dict]] = {}  # path -> (mtime, result)
_scan_cache_loaded_from_disk = False
_scan_cache_dirty = False  # set on any miss; parse_sessions() flushes once per cycle


def _load_scan_cache_from_disk() -> None:
    """One-time load of a prior process's scan results, keyed by (path,
    mtime) same as the in-memory cache: a cold monitor launch or restart
    otherwise re-parses every transcript's JSONL from scratch even when
    nothing on disk has changed since the last run. Measured 2026-08-16
    at ~5s of a ~6.5s cold parse_sessions() against real session counts,
    the dominant remaining cost after Ctrl+Shift+N's fast-path handoff
    made the "monitor already running" case fast; this is what's left for
    the "no monitor running yet" case (a fresh launch or restart)."""
    global _scan_cache, _scan_cache_loaded_from_disk
    if _scan_cache_loaded_from_disk:
        return
    _scan_cache_loaded_from_disk = True
    try:
        raw = json.loads(SCAN_CACHE_PATH.read_text())
        _scan_cache = {path: (entry[0], entry[1]) for path, entry in raw.items()}
    except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError):
        pass


def _save_scan_cache_to_disk() -> None:
    """Pruned to transcripts that still exist on disk before writing:
    without this, monitor-scan-cache.json only ever grows, across every
    restart, for as long as the tool is used, retaining scan results
    (including last_assistant_text snippets) for transcripts long since
    deleted. os.path.exists() is a cheap stat, negligible next to the
    json.loads() cost this cache exists to avoid (caught by review,
    2026-08-17)."""
    try:
        SCAN_CACHE_PATH.write_text(json.dumps(
            {path: [mtime, result] for path, (mtime, result) in _scan_cache.items()
             if os.path.exists(path)}
        ))
    except OSError:
        pass


def scan_full_file(path: str, stale_ok: bool = False) -> dict:
    """Single-pass full file scan: tokens, MCP, title, slug, created, last activity.

    Results are cached by (path, mtime): unchanged files return instantly.
    When stale_ok=True, returns the last cached result even if mtime changed
    (hook state provides fresher status/tool data; tokens/model are slow-changing).
    The cache itself is loaded from disk once per process (a prior run's
    results), so a fresh launch or Shift+R restart doesn't re-parse every
    transcript from scratch just because this is a new Python process.
    """
    _load_scan_cache_from_disk()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    cached = _scan_cache.get(path)
    if cached and (cached[0] == mtime or stale_ok):
        return cached[1]

    result = {
        "custom_title": "", "slug": "", "mcp_calls": 0,
        "tokens_in": 0, "tokens_out": 0,
        "last_input_tokens": 0,
        "model_id": "", "created": 0.0, "last_assistant_time": 0.0,
        "cwd": "", "last_tool": "", "last_tool_input": {},
        "last_assistant_text": "", "message_count": 0,
        "entrypoint": "",
    }

    try:
        with open(path, "r") as f:
            for line in f:
                if not line.strip():
                    continue

                if not result["entrypoint"] and '"entrypoint"' in line:
                    try:
                        msg = json.loads(line)
                        ep = msg.get("entrypoint", "")
                        if ep:
                            result["entrypoint"] = ep
                    except json.JSONDecodeError:
                        pass

                # Fast string checks before JSON parse
                if '"custom-title"' in line:
                    try:
                        msg = json.loads(line)
                        if msg.get("type") == "custom-title":
                            result["custom_title"] = msg.get("customTitle", "")
                    except json.JSONDecodeError:
                        pass
                    continue

                if '"mcp__' in line:
                    result["mcp_calls"] += line.count('"mcp__')

                # Only parse lines that could be assistant messages or have useful data
                if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
                    # Check for slug and cwd in non-assistant lines too
                    if '"slug"' in line:
                        try:
                            msg = json.loads(line)
                            s = msg.get("slug", "")
                            if s:
                                result["slug"] = s
                        except json.JSONDecodeError:
                            pass
                    if '"cwd"' in line:
                        try:
                            msg = json.loads(line)
                            if msg.get("cwd"):
                                result["cwd"] = msg["cwd"]
                            # Get created from first message with timestamp
                            if result["created"] == 0.0 and msg.get("timestamp"):
                                result["created"] = parse_timestamp(msg["timestamp"])
                        except json.JSONDecodeError:
                            pass
                    if '"type":"user"' in line or '"type": "user"' in line:
                        result["message_count"] += 1
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if msg.get("cwd"):
                    result["cwd"] = msg["cwd"]
                if msg.get("slug"):
                    result["slug"] = msg["slug"]
                if result["created"] == 0.0 and msg.get("timestamp"):
                    result["created"] = parse_timestamp(msg["timestamp"])

                ts = msg.get("timestamp", "")
                if ts:
                    t = parse_timestamp(ts)
                    if t > result["last_assistant_time"]:
                        result["last_assistant_time"] = t

                inner = msg.get("message", {})
                m = inner.get("model", "")
                if m:
                    result["model_id"] = m

                usage = inner.get("usage", {})
                if usage:
                    result["tokens_in"] += usage.get("input_tokens", 0)
                    result["tokens_in"] += usage.get("cache_read_input_tokens", 0)
                    result["tokens_in"] += usage.get("cache_creation_input_tokens", 0)
                    result["tokens_out"] += usage.get("output_tokens", 0)
                    # Current context = all input tokens for this API call
                    ctx = (usage.get("input_tokens", 0)
                           + usage.get("cache_read_input_tokens", 0)
                           + usage.get("cache_creation_input_tokens", 0))
                    if ctx > 0:
                        result["last_input_tokens"] = ctx

                # Extract last tool use and last text
                content = inner.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            name = block.get("name", "")
                            result["last_tool"] = name
                            inp = block.get("input", {})
                            if isinstance(inp, dict):
                                result["last_tool_input"] = inp
                        elif block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                result["last_assistant_text"] = text[:500]

                result["message_count"] += 1

    except OSError:
        pass

    _scan_cache[path] = (mtime, result)
    global _scan_cache_dirty
    _scan_cache_dirty = True
    return result


_subagent_cache: dict[str, tuple[float, int, list[Path]]] = {}  # dir -> (mtime, compacts, paths)


def _scan_subagent_dir(parent_path: str) -> tuple[int, list[Path]]:
    """Scan subagent directory, cached by directory mtime."""
    parent = Path(parent_path)
    subagent_dir = parent.parent / parent.stem / "subagents"
    if not subagent_dir.exists():
        return 0, []
    try:
        mtime = subagent_dir.stat().st_mtime
    except OSError:
        return 0, []
    key = str(subagent_dir)
    cached = _subagent_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]

    all_jsonl = sorted(subagent_dir.glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    compacts = sum(1 for p in all_jsonl if p.name.startswith("agent-acompact-"))
    _subagent_cache[key] = (mtime, compacts, all_jsonl)
    return compacts, all_jsonl


def count_compactions(parent_path: str) -> int:
    return _scan_subagent_dir(parent_path)[0]


def find_subagent_paths(parent_path: str) -> list[Path]:
    return _scan_subagent_dir(parent_path)[1]


_BACKGROUND_WINDOW_S = 15.0


def count_background_activity(transcript_path: str) -> int:
    """How many subagent/workflow transcripts under this session were written
    to in the last _BACKGROUND_WINDOW_S seconds — i.e., spawned work that is
    still running while the main loop sits idle. Workflow agents nest as
    subagents/workflows/wf_<id>/agent-*.jsonl, so this must recurse."""
    p = Path(transcript_path)
    base = p.parent / p.stem
    now = time.time()
    n = 0
    for sub in ("subagents", "workflows"):
        d = base / sub
        if not d.is_dir():
            continue
        try:
            for f in d.rglob("*.jsonl"):
                try:
                    if now - f.stat().st_mtime < _BACKGROUND_WINDOW_S:
                        n += 1
                except OSError:
                    continue
        except OSError:
            continue
    return n


@dataclass
class Task:
    id: str
    subject: str
    status: str  # pending, in_progress, completed, deleted
    active_form: str = ""


def load_tasks(session_id: str) -> list[Task]:
    """Load tasks for a session from ~/.claude/tasks/{session_id}/."""
    task_dir = TASKS_DIR / session_id
    if not task_dir.exists():
        return []
    tasks = []
    for f in task_dir.iterdir():
        if not f.suffix == ".json":
            continue
        try:
            data = json.loads(f.read_text())
            status = data.get("status", "pending")
            if status == "deleted":
                continue
            tasks.append(Task(
                id=data.get("id", f.stem),
                subject=data.get("subject", ""),
                status=status,
                active_form=data.get("activeForm", ""),
            ))
        except (json.JSONDecodeError, OSError):
            continue
    # Sort by ID (numeric)
    tasks.sort(key=lambda t: int(t.id) if t.id.isdigit() else 0)
    return tasks


def format_plan(tasks: list[Task], max_lines: int = 8) -> str:
    """Format tasks as a Rich-markup plan checklist."""
    if not tasks:
        return ""
    completed = sum(1 for t in tasks if t.status == "completed")
    total = len(tasks)
    in_progress = [t for t in tasks if t.status == "in_progress"]

    header = f"[bold]Plan[/] [dim]{completed}/{total} done[/]"
    if in_progress:
        current = in_progress[0].active_form or in_progress[0].subject
        header += f"  [cyan]→ {current}[/]"

    lines = [header]
    for t in tasks[:max_lines]:
        subj = _escape_markup(t.subject[:50])
        if t.status == "completed":
            lines.append(f"  [green]✓[/] [dim]{subj}[/]")
        elif t.status == "in_progress":
            lines.append(f"  [cyan]▸[/] [bold]{subj}[/]")
        else:
            lines.append(f"  [dim]○[/] {subj}")
    if total > max_lines:
        lines.append(f"  [dim]… +{total - max_lines} more[/]")
    return "\n".join(lines)


def load_index_metadata() -> dict[str, dict]:
    meta = {}
    if not CLAUDE_DIR.exists():
        return meta
    for index_file in CLAUDE_DIR.rglob("sessions-index.json"):
        try:
            data = json.loads(index_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        project_path = data.get("originalPath", "")
        project_name = Path(project_path).name if project_path else "~"
        for entry in data.get("entries", data.get("sessions", [])):
            sid = entry.get("sessionId", "")
            if sid:
                meta[sid] = {
                    "project": project_name,
                    "summary": entry.get("summary", ""),
                    "firstPrompt": entry.get("firstPrompt", ""),
                    "messageCount": entry.get("messageCount", 0),
                    "projectPath": entry.get("projectPath", project_path),
                }
    return meta


def estimate_cost(model_id: str, tokens_in: int, tokens_out: int) -> float:
    for k, (ip, op) in MODEL_PRICING.items():
        if k in model_id:
            return (tokens_in / 1_000_000 * ip) + (tokens_out / 1_000_000 * op)
    return 0.0


_gc_state_files_last: float = 0


def _gc_state_files() -> None:
    """Delete hook state files for sessions exited >24h ago. Runs hourly."""
    global _gc_state_files_last
    now = time.time()
    if now - _gc_state_files_last < 3600:
        return
    _gc_state_files_last = now
    if not HOOK_STATE_DIR.exists():
        return
    cutoff = now - 86400
    for f in HOOK_STATE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("state") != "exited":
                continue
            exited_at = data.get("exited_at", "")
            if exited_at and parse_timestamp(exited_at) < cutoff:
                f.unlink()
        except (json.JSONDecodeError, OSError, ValueError):
            pass


_PERF = os.environ.get("CLAUDE_MONITOR_PERF") == "1"
def _perf(label: str, t0: float) -> float:
    """Print elapsed time since t0 and return new perf_counter()."""
    if _PERF:
        print(f"[perf] {label}: {(time.perf_counter()-t0)*1000:.1f}ms", file=sys.stderr)
    return time.perf_counter()


def parse_sessions(include_archived: bool = False,
                   include_subagents: bool = False,
                   pinned: set[str] | None = None) -> list[Session]:
    t0 = time.perf_counter()
    sessions = []
    pinned = pinned or set()
    now = time.time()
    active_cutoff = now - 86400
    archive_cutoff = now - 86400 * 7  # 7 days for archived

    if not CLAUDE_DIR.exists():
        return sessions

    _gc_state_files()
    t0 = _perf("  parse_sessions: _gc_state_files", t0)

    meta = load_index_metadata()
    t0 = _perf("  parse_sessions: load_index_metadata", t0)

    n_scanned = n_full_scan = n_stale_ok = n_alive_check = 0
    t_rglob = t_build = t_compact = t_subagent = 0.0
    tg = time.perf_counter()
    for jsonl_path in CLAUDE_DIR.rglob("*.jsonl"):
        if "subagents" in str(jsonl_path):
            continue
        try:
            mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue

        t_rglob += time.perf_counter() - tg
        session_id = jsonl_path.stem
        is_pinned = session_id in pinned

        # A pin is a permanent exemption from every age filter here. It stays
        # until you unpin it, full stop (Max: "pins should stay until I unpin
        # them"). Whether an inactive pin actually SHOWS in the default view
        # is a separate, simpler question: the hide_inactive_pins toggle in
        # _refresh_compute() answers it without touching age at all.
        is_archived = mtime < active_cutoff
        if is_archived and not include_archived and not is_pinned:
            if not _is_session_alive(session_id):
                tg = time.perf_counter()
                continue
            is_archived = False
        if mtime < archive_cutoff and not _is_session_alive(session_id) and not is_pinned:
            tg = time.perf_counter()
            continue
        idx = meta.get(session_id, {})
        project = idx.get("project", jsonl_path.parent.name.split("-")[-1] or "~")

        n_scanned += 1
        # Track if scan_full_file will hit cache or do full read
        cached = _scan_cache.get(str(jsonl_path))
        hook = read_hook_state(session_id)
        hook_fresh = False
        if hook and hook.get("timestamp"):
            try:
                hook_fresh = (time.time() - parse_timestamp(hook["timestamp"])) < 30
            except (ValueError, TypeError):
                pass
        if cached and (cached[0] == mtime or hook_fresh):
            n_stale_ok += 1
        else:
            n_full_scan += 1

        tb = time.perf_counter()
        session = build_session(str(jsonl_path), session_id, project, idx, mtime)
        t_build += time.perf_counter() - tb
        if session:
            # Hide ghost sessions: ≤20 output tokens = just the greeting
            # Keep only if we can confirm a live process.
            # Guard: if the file is large (>50KB) but tokens_out is still
            # low, the scan cache is stale — force a rescan before filtering.
            if session.tokens_out <= 20:
                try:
                    fsize = jsonl_path.stat().st_size
                except OSError:
                    fsize = 0
                if fsize > 50_000 and _scan_cache.get(str(jsonl_path)):
                    del _scan_cache[str(jsonl_path)]
                    data = scan_full_file(str(jsonl_path))
                    session.tokens_out = data.get("tokens_out", 0)
                    session.tokens_in = data.get("tokens_in", 0)
                if session.tokens_out <= 20 and not is_pinned:
                    n_alive_check += 1
                    if _is_session_alive(session_id) is not True:
                        tg = time.perf_counter()
                        continue
            if is_archived:
                session.status = "archived"
            tc = time.perf_counter()
            session.compact_count = count_compactions(str(jsonl_path))
            t_compact += time.perf_counter() - tc
            if include_subagents and not is_archived:
                ts = time.perf_counter()
                for sub_path in find_subagent_paths(str(jsonl_path)):
                    sub = build_session(
                        str(sub_path), sub_path.stem, project, {},
                        sub_path.stat().st_mtime, is_subagent=True, parent_id=session_id,
                    )
                    if sub:
                        session.subagents.append(sub)
                t_subagent += time.perf_counter() - ts
            sessions.append(session)
        tg = time.perf_counter()

    # Second pass: discover alive sessions from PID files that have no transcript
    _refresh_pid_map()
    found_sids = {s.session_id for s in sessions}
    n_orphans = 0
    for sid, pid in _pid_map.items():
        if pid is None or sid in found_sids:
            continue
        # Read the PID file for metadata
        pid_file = SESSIONS_DIR / f"{pid}.json"
        try:
            pdata = json.loads(pid_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if pdata.get("kind") != "interactive":
            continue
        # Skip sessions still booting — PID file lands before name/transcript,
        # which would surface as an unactionable "Claude" / WORKING / 0% row.
        # They appear correctly on the next 2s refresh once the transcript exists.
        started_ms = pdata.get("startedAt", 0)
        if not pdata.get("name") and started_ms and (time.time() - started_ms / 1000.0) < 5:
            continue
        # Build title from best available source
        hook = read_hook_state(sid)
        sl_name = _read_session_cache("name", sid)
        title = (
            pdata.get("name")
            or sl_name
            or (hook.get("title") if hook else "")
            or "Claude"
        )
        status = "done"
        if hook:
            hs = hook.get("state", "")
            if hs == "thinking":
                status = "working"
            elif hs == "approval":
                status = "needs_approval"
            elif hs == "exited":
                status = "closed"
        created = pdata.get("startedAt", 0) / 1000.0
        updated = pdata.get("updatedAt", 0) / 1000.0
        cwd = pdata.get("cwd", "")
        project = Path(cwd).name if cwd and Path(cwd).name != Path.home().name else "~"
        session = Session(
            session_id=sid,
            project=project,
            title=title,
            status=status,
            model="",
            model_id="",
            cost=0.0,
            tokens_in=0,
            tokens_out=0,
            context_pct=0,
            message_count=0,
            last_activity=updated or created or now,
            created=created or now,
            cwd=cwd,
            transcript_path="",
            status_name=sl_name or title,
        )
        sessions.append(session)
        n_orphans += 1

    # Third pass: split shared-tree sessions into one row per PID. A transcript
    # is a parentUuid tree, not a linear log — two `--resume`s of one sid sit
    # on divergent branches in the SAME file. Monitor's transcript-derived row
    # represents the whole tree, not either branch, so when N>1 PIDs claim a
    # sid we replace that row with N PID-derived siblings whose title/status
    # come from each sessions/{pid}.json (the only per-branch source we have).
    sid_pids: dict[str, list[dict]] = {}
    if SESSIONS_DIR.is_dir():
        for path in SESSIONS_DIR.iterdir():
            if path.suffix != ".json":
                continue
            try:
                pdata = json.loads(path.read_text())
                pid = int(pdata.get("pid", 0))
                sid = pdata.get("sessionId", "")
                if not (pid and sid):
                    continue
                os.kill(pid, 0)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if pdata.get("kind") != "interactive":
                continue
            sid_pids.setdefault(sid, []).append(pdata)

    by_sid = {s.session_id: s for s in sessions}
    for sid, pids in sid_pids.items():
        if len(pids) < 2 or sid not in by_sid:
            continue
        base = by_sid[sid]
        sessions.remove(base)
        for pdata in sorted(pids, key=lambda p: p.get("startedAt", 0)):
            pid = pdata["pid"]
            updated = pdata.get("updatedAt", 0) / 1000.0
            pstatus = pdata.get("status", "")
            sib = replace(
                base,
                session_id=f"{sid}@{pid}",
                title=pdata.get("name") or base.title,
                status=("working" if pstatus == "busy" else
                        "done" if pstatus == "idle" else base.status),
                last_activity=updated or base.last_activity,
                context_is_estimate=True,
                status_name=pdata.get("name") or base.status_name,
                # Each PID is its own instance: derive the durable surrogate from
                # this pid's own startedAt, not the base's. replace() would
                # otherwise copy one instance_id onto every sibling, tripping the
                # dup_key invariant (caught live by the audit on a real
                # double-resume of one conversation).
                sid=sid,
                instance_id=f"{sid}{INSTANCE_SEP}{pdata.get('startedAt', 0)}",
            )
            sessions.append(sib)

    if _PERF:
        print(f"[perf]   parse_sessions: rglob iter: {t_rglob*1000:.1f}ms", file=sys.stderr)
        print(f"[perf]   parse_sessions: build_session: {t_build*1000:.1f}ms "
              f"({n_scanned} sessions, {n_full_scan} full scans, {n_stale_ok} cached/stale-ok)", file=sys.stderr)
        print(f"[perf]   parse_sessions: count_compactions: {t_compact*1000:.1f}ms", file=sys.stderr)
        print(f"[perf]   parse_sessions: subagents: {t_subagent*1000:.1f}ms", file=sys.stderr)
        print(f"[perf]   parse_sessions: _is_session_alive checks: {n_alive_check}", file=sys.stderr)
        if n_orphans:
            print(f"[perf]   parse_sessions: PID-file orphans added: {n_orphans}", file=sys.stderr)

    sessions = filter_ignored(sessions)

    # STANDBY for config-* desks, applied ONCE here over every session
    # regardless of which construction path produced it. It was first wired
    # into build_session() only, which missed two other places that assign
    # status: the PID-file orphan pass (hardcodes "done" for a session with
    # no transcript yet) and the multi-PID sibling split (maps idle ->
    # "done", overriding the base row). A double-resumed config-MCPs, the
    # exact case the sibling pass exists for, therefore still rendered
    # bold READY and re-entered Ctrl+Shift+N's candidate pool, which is
    # precisely what the feature is meant to stop (advisor review,
    # 2026-08-18). A single pass at the end cannot miss a path.
    _apply_standby_to_all(sessions)

    global _scan_cache_dirty
    if _scan_cache_dirty:
        _save_scan_cache_to_disk()
        _scan_cache_dirty = False

    return sessions


_STATUSLINE_CACHE_DIR = Path.home() / ".claude" / "statusline-cache"


def _read_session_cache(kind: str, session_id: str) -> str:
    """Read claude-{kind}-{session_id} from the persistent cache (survives
    reboot), falling back to /tmp for sessions whose statusline predates the
    persistent dir. Returns stripped string or empty."""
    for base in (_STATUSLINE_CACHE_DIR, Path("/tmp")):
        try:
            v = (base / f"claude-{kind}-{session_id}").read_text().strip()
            if v:
                return v
        except OSError:
            continue
    return ""


def read_session_memory_title(transcript_path: str) -> str:
    """Read session title from session-memory/summary.md next to the transcript."""
    base = transcript_path
    if base.endswith(".jsonl"):
        base = base[:-6]
    summary_path = Path(base) / "session-memory" / "summary.md"
    try:
        if not summary_path.exists():
            return ""
        in_title_section = False
        for line in summary_path.read_text().splitlines():
            if line.strip() == "# Session Title":
                in_title_section = True
                continue
            if in_title_section:
                stripped = line.strip()
                if not stripped or stripped.startswith("_"):
                    continue
                if stripped.startswith("#"):
                    break
                return stripped
    except OSError:
        pass
    return ""


def build_session(path: str, session_id: str, project: str, idx: dict,
                  mtime: float, is_subagent: bool = False,
                  parent_id: str = "") -> Session | None:
    # If hook state is fresh (<30s), skip transcript rescan — tokens/model
    # are slow-changing and stale cache is fine. Hook provides status/tool.
    hook = None if is_subagent else read_hook_state(session_id)
    hook_fresh = False
    if hook and hook.get("timestamp"):
        try:
            hook_age = time.time() - parse_timestamp(hook["timestamp"])
            hook_fresh = hook_age < 30
        except (ValueError, TypeError):
            pass

    data = scan_full_file(path, stale_ok=hook_fresh)

    # Compute display title — priority chain:
    # 1. custom_title (set by user in session)
    # 2. hook state title (session-memory or Haiku-generated)
    # 3. sessions-index summary
    # 4. session-memory/summary.md (direct read)
    # 5. first prompt / cwd / session_id fallback
    if is_subagent:
        parts = Path(path).stem.split("-")
        display_title = "-".join(parts[:2]) if len(parts) >= 2 else session_id[:12]
    else:
        hook_title = hook.get("title", "") if hook else ""
        # A transcript is a parentUuid TREE, not a line. scan_full_file returns
        # the LAST custom-title in the file, which for a long-lived sid that was
        # resumed across projects can be a straggler from a divergent branch
        # (e.g. a month-long "tools-frontier-curve" session whose final written
        # leaf briefly carried "tools-monitor"). For an EXITED session the hook
        # state captured the settled identity it carried during its working life,
        # i.e. what the user actually saw and pinned, so trust that over the
        # straggler. Live sessions keep custom_title first so /rename still wins.
        hook_exited = bool(hook) and hook.get("state") == "exited"
        if hook_exited and hook_title:
            display_title = (
                hook_title
                or data["custom_title"]
                or idx.get("summary", "")
                or read_session_memory_title(path)
                or idx.get("firstPrompt", "")[:60]
                or Path(data["cwd"]).name
                or session_id[:8]
            )
        else:
            display_title = (
                data["custom_title"]
                or hook_title
                or idx.get("summary", "")
                or read_session_memory_title(path)
                or idx.get("firstPrompt", "")[:60]
                or Path(data["cwd"]).name
                or session_id[:8]
            )

    status = determine_status(session_id, data["last_assistant_time"], display_title, path)
    bg_count = count_background_activity(path) if status == "working" else 0

    # Context %: how much context is USED (burnt).
    # Statusline cache stores remaining %, so we flip it.
    context_is_estimate = False
    try:
        remaining = int(float(_read_session_cache("ctx", session_id)))
        context_pct = max(0, min(100, 100 - remaining))
    except (ValueError, TypeError):
        context_is_estimate = True
        last_input = data["last_input_tokens"]
        if last_input == 0:
            context_pct = 0  # Nothing used yet
        else:
            window = model_context_window(data["model_id"], observed_tokens=last_input)
            context_pct = min(100, int((last_input / window) * 100))

    # Prefer ground-truth cost from statusline cache, fall back to estimation
    cached_cost = _read_session_cache("cost", session_id)
    if cached_cost:
        try:
            cost = float(cached_cost)
        except ValueError:
            cost = estimate_cost(data["model_id"], data["tokens_in"], data["tokens_out"])
    else:
        cost = estimate_cost(data["model_id"], data["tokens_in"], data["tokens_out"])

    remote_url = ""
    # Slug: prefer live cache from statusline, fall back to transcript
    slug = data["slug"]
    cached_url = _read_session_cache("url", session_id)
    if "/session_" in cached_url:
        slug = cached_url.split("/session_", 1)[1]

    if slug and not is_subagent:
        remote_url = f"https://claude.ai/code/session_{slug}"

    status_name = _read_session_cache("name", session_id)

    # SHADOW MODE: run the resolver alongside the current logic (observe-only).
    # Rendering still uses the old derivation; the resolver only logs where it
    # WOULD diverge, and populates the new sid/instance_id carrier fields. This
    # surfaces real-world identity bugs before the cutover and feeds the
    # crystallization loop with zero risk to what is displayed.
    _sid_val, _iid_val = session_id, ""
    if not is_subagent:
        try:
            ri = resolve_session(session_id, data, idx, path)
            _sid_val, _iid_val = ri.sid, ri.instance_id
            # Compare TITLE only. Status is computed by determine_status, which
            # reads wall-clock time + live transcript mtime; calling it twice in
            # one build cycle (once for `status`, once inside resolve_session)
            # races across the 5s/30s thresholds and yields benign working vs
            # background/waiting flips. That is shadow-only noise: the cutover
            # computes status once. Title is the deterministic signal worth logging.
            if ri.title != display_title[:50]:
                from monitor_log import log as _shadow_log
                _shadow_log("shadow", "divergence", sid=session_id[:8],
                            cur_title=display_title[:50], new_title=ri.title,
                            new_title_source=ri.title_source,
                            origin=ri.origin, iid=ri.instance_id)
        except Exception as _shadow_err:
            try:
                from monitor_log import log as _shadow_log
                _shadow_log("shadow", "error", sid=session_id[:8],
                            err=str(_shadow_err))
            except Exception:
                pass

    return Session(
        session_id=session_id, project=project,
        title=display_title[:50], status=status,
        model=format_model(data["model_id"]), model_id=data["model_id"],
        cost=cost, tokens_in=data["tokens_in"], tokens_out=data["tokens_out"],
        context_pct=context_pct,
        context_is_estimate=context_is_estimate,
        message_count=data["message_count"] or idx.get("messageCount", 0),
        last_activity=mtime, created=data["created"],
        cwd=data["cwd"], transcript_path=path,
        remote_url=remote_url, slug=slug,
        is_subagent=is_subagent, parent_id=parent_id,
        mcp_calls=data["mcp_calls"],
        last_tool=data["last_tool"],
        last_tool_input=data["last_tool_input"],
        last_assistant_text=data["last_assistant_text"],
        status_name=status_name,
        project_path=idx.get("projectPath", ""),
        background_count=bg_count,
        is_scheduled=(data.get("entrypoint") not in ("", "cli")),
        sid=_sid_val, instance_id=_iid_val,
    )


# PID map: built once per refresh cycle to avoid re-scanning ~/.claude/sessions/
# per-session. Maps sessionId -> alive PID (int) or None (dead/not found).
_pid_map: dict[str, int | None] = {}
_pid_map_ts: float = 0


def _refresh_pid_map() -> None:
    """Rebuild the PID map from ~/.claude/sessions/*.json files."""
    global _pid_map, _pid_map_ts
    now = time.time()
    if now - _pid_map_ts < 2:
        return
    _pid_map = {}
    if SESSIONS_DIR.is_dir():
        for path in SESSIONS_DIR.iterdir():
            if path.suffix != ".json":
                continue
            try:
                data = json.loads(path.read_text())
                sid = data.get("sessionId", "")
                pid = int(data["pid"])
                try:
                    os.kill(pid, 0)
                    _pid_map[sid] = pid  # Alive
                except OSError:
                    if sid not in _pid_map:  # Don't overwrite alive with dead
                        _pid_map[sid] = None
            except (json.JSONDecodeError, OSError, KeyError, ValueError):
                continue
    _pid_map_ts = now


_recently_resumed: dict[str, float] = {}  # sid -> timestamp (set by resume_session)
_RESUME_GRACE = 60  # seconds to treat a resumed session as alive without a PID

# One "ps -Ao pid,comm=" snapshot per refresh cycle instead of a "ps -p <pid>"
# subprocess per session: profiled 2026-08-16 against 136 real sessions,
# 236 individual spawns cost ~1.5s of parse_sessions()'s ~6.5s cold-scan
# total, the single biggest fixable chunk after the JSONL parse itself.
_process_comm_cache: dict[int, str] = {}
_process_comm_ts: float = 0


def _refresh_process_comm_cache() -> None:
    global _process_comm_cache, _process_comm_ts
    now = time.time()
    if now - _process_comm_ts < 2:
        return
    cache: dict[int, str] = {}
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,comm="],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_str, _, comm = line.partition(" ")
            try:
                cache[int(pid_str)] = comm.strip().lower()
            except ValueError:
                continue
    except (subprocess.SubprocessError, OSError):
        return  # Keep the stale cache rather than wiping it on a transient failure.
    _process_comm_cache = cache
    _process_comm_ts = now


def _pid_is_claude(pid: int) -> bool:
    """True iff PID is alive AND is a claude CLI process — guards against
    recycled PIDs where an old session's PID now belongs to e.g. mdworker."""
    _refresh_process_comm_cache()
    comm = _process_comm_cache.get(pid, "")
    return ("claude" in comm and "monitor" not in comm
            and "helper" not in comm and "crashpad" not in comm
            and ".app" not in comm)


def _is_session_alive(session_id: str, display_title: str = "") -> bool:
    """Check if the Claude process for this session is still running.

    Primary: PID files in ~/.claude/sessions/.
    Grace period: sessions resumed from the monitor are treated as alive
    for 60s while waiting for the PID file to appear.
    """
    _refresh_pid_map()

    if session_id in _pid_map:
        pid = _pid_map[session_id]
        if pid is not None:
            _recently_resumed.pop(session_id, None)  # PID appeared, no longer need grace
            return True
        # pid is None → stale/dead PID file. Don't return False yet —
        # fall through to the grace-period check so a freshly resumed
        # session isn't flickered as closed before its new PID file lands.

    # No live PID file — check hook state as an alternative PID source.
    # Some sessions don't create ~/.claude/sessions/*.json PID files
    # but the hook still tracks them with a valid PID.
    hook = read_hook_state(session_id)
    if hook and hook.get("pid"):
        try:
            pid = int(hook["pid"])
            if _pid_is_claude(pid):
                # The PID is a live claude — but after /branch, or once the
                # PID has simply been recycled to an unrelated session, the
                # SAME PID now serves a different sid. Cross-check the
                # canonical PID→sessionId map; if it disagrees, this hook
                # lead is a dead end — fall through to the grace-period
                # check below rather than asserting dead outright (a stale
                # hook pid must never override "we just resumed this",
                # or a rapid second resume looks alive-but-unreachable and
                # spawns a duplicate that gets kicked by CC's own
                # single-instance guard — observed live 2026-08-15,
                # config-MCPs, sid 8f7e4862).
                pid_file = SESSIONS_DIR / f"{pid}.json"
                now_serves = ""
                try:
                    pdata = json.loads(pid_file.read_text())
                    now_serves = pdata.get("sessionId", "")
                except (OSError, json.JSONDecodeError):
                    pass
                if now_serves and now_serves != session_id:
                    mlog("DIVERGE", "stale_hook_pid", sid=session_id[:12], pid=pid,
                         now_serves=now_serves[:12])
                else:
                    return True
        except ValueError:
            pass

    # Last resort: check if we just resumed this session
    resumed_at = _recently_resumed.get(session_id)
    if resumed_at and (time.time() - resumed_at) < _RESUME_GRACE:
        return True

    _recently_resumed.pop(session_id, None)
    return False


def _check_for_updates() -> None:
    """Fetch from origin and check if we're behind. Sets _update_available."""
    global _update_available
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main", "--quiet"],
            cwd=_REPO_DIR, capture_output=True, timeout=15,
        )
        result = subprocess.run(
            ["git", "rev-list", "HEAD..origin/main", "--count"],
            cwd=_REPO_DIR, capture_output=True, text=True, timeout=5,
        )
        count = int(result.stdout.strip() or "0")
        if count > 0:
            log_result = subprocess.run(
                ["git", "log", "origin/main", f"-{min(count, 3)}", "--pretty=%s"],
                cwd=_REPO_DIR, capture_output=True, text=True, timeout=5,
            )
            changes = log_result.stdout.strip().split("\n")
            _update_available = changes[0] if changes else "New update available"
            mlog("update", "available", commits=count, latest=_update_available)
        else:
            _update_available = ""
    except (subprocess.SubprocessError, OSError, ValueError):
        pass


@dataclass
class Discrepancy:
    kind: str
    sid: str
    details: dict


def _read_pid_file(pid: int) -> dict:
    try:
        return json.loads((SESSIONS_DIR / f"{pid}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _pids_claiming_sid(sid: str) -> list[int]:
    out: list[int] = []
    if not SESSIONS_DIR.is_dir():
        return out
    for f in SESSIONS_DIR.iterdir():
        if f.suffix != ".json":
            continue
        try:
            d = json.loads(f.read_text())
            if d.get("sessionId") == sid:
                out.append(int(d.get("pid", f.stem)))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return out


def _read_transcript_title(sid: str) -> str:
    for p in CLAUDE_DIR.glob(f"*/{sid}.jsonl"):
        title = ""
        try:
            with p.open("rb") as fh:
                try:
                    fh.seek(-65536, 2)
                except OSError:
                    fh.seek(0)
                for raw in fh.read().splitlines():
                    line = raw.decode("utf-8", "ignore")
                    if '"custom-title"' not in line:
                        continue
                    try:
                        m = json.loads(line)
                        if m.get("type") == "custom-title":
                            title = m.get("customTitle", "") or title
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
        return title
    return ""


def reconcile_sources(sid: str) -> list[Discrepancy]:
    """Cross-check the five identity sources for one session and return any
    disagreements. The point is to surface desyncs as data, not to wait for
    them to manifest as a UI ghost."""
    out: list[Discrepancy] = []
    hook = read_hook_state(sid) or {}
    claiming = _pids_claiming_sid(sid)

    # multi_pid_same_sid — two terminals both think they host this conversation
    live = [p for p in claiming if _pid_is_claude(p)]
    if len(live) > 1:
        out.append(Discrepancy("multi_pid_same_sid", sid, {"pids": live}))

    # pid_mismatch — hook says PID X but X now serves a different sid
    hpid = hook.get("pid")
    if hpid:
        try:
            hpid = int(hpid)
            pdata = _read_pid_file(hpid)
            psid = pdata.get("sessionId")
            if psid and psid != sid and _pid_is_claude(hpid):
                out.append(Discrepancy("pid_mismatch", sid,
                                       {"hook_pid": hpid, "pid_now_serves": psid}))
        except (ValueError, TypeError):
            pass

    # liveness_mismatch — hook recorded "exited" but the same PID still serves this sid
    if hook.get("state") == "exited" and hpid and _pid_is_claude(int(hpid)):
        pdata = _read_pid_file(int(hpid))
        if pdata.get("sessionId") == sid:
            out.append(Discrepancy("liveness_mismatch", sid,
                                   {"hook_state": "exited", "pid": hpid}))

    # name_mismatch — transcript / statusline-cache / hook title disagree
    t_title = _read_transcript_title(sid)
    cache_name = _read_session_cache("name", sid)
    h_title = hook.get("title") or ""
    names = {n for n in (t_title, cache_name, h_title) if n}
    if len(names) > 1:
        out.append(Discrepancy("name_mismatch", sid,
                               {"transcript": t_title, "cache": cache_name,
                                "hook": h_title}))

    # orphan_state — hook-state file exists, no PID file claims it, hook PID dead
    if hook and not claiming:
        dead = (not hpid) or (not _pid_is_claude(int(hpid)) if str(hpid).isdigit() else True)
        if dead:
            out.append(Discrepancy("orphan_state", sid,
                                   {"hook_pid": hpid, "state": hook.get("state")}))

    return out


_last_reconcile_at = 0.0
_last_discrepancy_keys: set[str] = set()
_RECONCILE_MIN_INTERVAL = 60.0


def _reconcile_sessions() -> None:
    """Periodic sweep: heal stale hook states, refresh names from /tmp, and
    re-stamp terminal titles with ·sid8 markers. PID files are the authority
    for what's running — everything else is healed to match."""
    global _last_reconcile_at, _last_discrepancy_keys
    now = time.time()
    if now - _last_reconcile_at < _RECONCILE_MIN_INTERVAL:
        return
    _last_reconcile_at = now
    _refresh_pid_map()
    healed = stamped = 0
    for sid, pid in _pid_map.items():
        if pid is None:
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        # Heal hook state PID/TTY
        try:
            tty = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "tty="],
                text=True, timeout=2,
            ).strip()
        except (subprocess.SubprocessError, OSError):
            continue
        if not tty or tty == "??":
            continue
        state_file = HOOK_STATE_DIR / f"{sid}.json"
        try:
            data = json.loads(state_file.read_text()) if state_file.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        if data.get("pid") != pid or data.get("tty") != tty:
            data["pid"] = pid
            data["tty"] = tty
            data["session_id"] = sid
            try:
                state_file.write_text(json.dumps(data, indent=2) + "\n")
                _hook_state_cache.pop(sid, None)
                healed += 1
            except OSError:
                pass
        # Refresh title from /tmp name file (statusline writes this)
        title = data.get("title", "")
        name_file = Path(f"/tmp/claude-name-{sid}")
        try:
            sl_name = name_file.read_text().strip() if name_file.exists() else ""
        except OSError:
            sl_name = ""
        if sl_name and (not title or title == "Claude"):
            title = sl_name
            data["title"] = title
            data["title_source"] = "statusline"
            data["session_id"] = sid
            try:
                state_file.write_text(json.dumps(data, indent=2) + "\n")
                _hook_state_cache.pop(sid, None)
                healed += 1
            except OSError:
                pass
        # Re-stamp terminal title
        tty_path = Path(f"/dev/{tty}")
        if tty_path.exists():
            sid8 = sid[:8]
            name = title[:31] + "\u2026" if len(title) > 32 else (title or "Claude")
            try:
                with open(tty_path, "w") as f:
                    f.write(f"\x1b]2;\u2733 {name} \u00b7{sid8}\x07")
                stamped += 1
            except OSError:
                pass
    # Detect cross-source identity desyncs and log them; heal orphan_state.
    # Only check sessions that are actually live or recently were — not every
    # hook-state file ever written.
    seen_sids = set(_pid_map)
    discrepancies = 0
    pruned = 0
    new_keys: set[str] = set()
    for sid in seen_sids:
        for d in reconcile_sources(sid):
            discrepancies += 1
            key = f"{sid}:{d.kind}:{sorted(d.details.items())}"
            new_keys.add(key)
            if key not in _last_discrepancy_keys:
                mlog("reconcile", d.kind, sid=sid, **d.details)
            if d.kind == "orphan_state":
                sf = HOOK_STATE_DIR / f"{sid}.json"
                try:
                    if sf.exists() and (now - sf.stat().st_mtime) > 3600:
                        sf.unlink()
                        _hook_state_cache.pop(sid, None)
                        pruned += 1
                except OSError:
                    pass
    resolved = _last_discrepancy_keys - new_keys
    for key in resolved:
        sid, kind = key.split(":", 2)[:2]
        mlog("reconcile", "resolved", sid=sid, kind=kind)
    _last_discrepancy_keys = new_keys
    if discrepancies or pruned or resolved:
        mlog("reconcile", "sweep", healed=healed, stamped=stamped,
             discrepancies=discrepancies, orphans_pruned=pruned,
             resolved=len(resolved))


def run_reconcile_report() -> int:
    """CLI entry: print all source discrepancies and exit with count."""
    _refresh_pid_map()
    seen_sids = set(_pid_map) | {p.stem for p in HOOK_STATE_DIR.glob("*.json")}
    total = 0
    for sid in sorted(seen_sids):
        ds = reconcile_sources(sid)
        for d in ds:
            total += 1
            detail = " ".join(f"{k}={v}" for k, v in d.details.items())
            print(f"{d.kind:20} {sid[:8]}  {detail}")
    print(f"\n{total} discrepancies across {len(seen_sids)} sessions")
    return total


def _heal_hook_state(session_id: str) -> None:
    """Update hook state with correct PID/TTY from the PID file when the hook's
    own PID is stale. This happens when a session is resumed — the PID file gets
    a new entry but the hook state retains the old dead PID."""
    _refresh_pid_map()
    if session_id not in _pid_map or _pid_map[session_id] is None:
        return
    live_pid = _pid_map[session_id]
    try:
        tty = subprocess.check_output(
            ["ps", "-p", str(live_pid), "-o", "tty="],
            text=True, timeout=2,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return
    if not tty or tty == "??":
        return
    state_file = HOOK_STATE_DIR / f"{session_id}.json"
    try:
        data = json.loads(state_file.read_text()) if state_file.exists() else {}
        data["pid"] = live_pid
        data["tty"] = tty
        state_file.write_text(json.dumps(data, indent=2) + "\n")
        # Invalidate hook state cache
        _hook_state_cache.pop(session_id, None)
        mlog("heal", "hook_state_updated", sid=session_id[:12], pid=live_pid, tty=tty)
        # Re-stamp the OSC tab title with the ·sid8 marker so jump-to-terminal
        # can find it again (CC's own auto-title can clobber the hook stamp on
        # a background tab, leaving the session reachable by ps but not by
        # title; observed live 2026-06-21 as DIVERGE alive_but_unfound).
        name = data.get("title") or session_id[:8]
        if len(name) > 32:
            name = name[:31] + "…"
        title = f"✳ {name} ·{session_id[:8]}"
        with open(f"/dev/{tty}", "w") as t:
            t.write(f"\x1b]2;{title}\x07")
        mlog("heal", "osc_restamped", sid=session_id[:12], tty=tty, title=title)
    except (OSError, json.JSONDecodeError):
        pass


_hook_state_cache: dict[str, tuple[float, dict]] = {}  # session_id -> (mtime, data)


def read_hook_state(session_id: str) -> dict | None:
    """Read hook-written state file, cached by mtime."""
    state_file = HOOK_STATE_DIR / f"{session_id}.json"
    try:
        mtime = state_file.stat().st_mtime
    except OSError:
        return None
    cached = _hook_state_cache.get(session_id)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(state_file.read_text())
        _hook_state_cache[session_id] = (mtime, data)
        return data
    except (json.JSONDecodeError, OSError):
        return None


_THINKING_STALE_S = 180  # hook silence before we stop trusting "thinking"


def _thinking_is_stale(session_id: str, hook: dict, transcript_path: str) -> bool:
    """A hook 'thinking' state is an event-log entry, not a liveness signal:
    if the turn's Stop event never fires (slash-command / subagent /
    remote-bridge coverage gaps), the state freezes as thinking forever.
    Call it stale only when EVERY fresher witness disagrees:
      - the hook has been silent past _THINKING_STALE_S, AND
      - the transcript is not being appended to, AND
      - CC's own pid file does not say busy (it flips to idle itself).
    A genuinely long tool call keeps the transcript or pid-file status fresh,
    so real work is never demoted."""
    try:
        hook_age = time.time() - parse_timestamp(hook.get("timestamp", ""))
    except (ValueError, TypeError):
        return False
    if hook_age < _THINKING_STALE_S:
        return False
    if transcript_path:
        try:
            if time.time() - os.stat(transcript_path).st_mtime < _THINKING_STALE_S:
                return False
        except OSError:
            pass
    pid = _pid_map.get(session_id) or hook.get("pid")
    if pid:
        try:
            pdata = json.loads((SESSIONS_DIR / f"{pid}.json").read_text())
            if pdata.get("sessionId") == session_id and pdata.get("status") == "busy":
                return False
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return True


def _apply_standby_to_all(sessions: list["Session"]) -> None:
    """The one place standby is assigned: a final pass over every session
    parse_sessions() is about to return, whichever construction path built
    it (build_session, the PID-file orphan pass, the multi-PID sibling
    split). See _apply_standby_status for why config-* desks get it and
    the sibling-split bug that motivated doing this in one final pass."""
    for s in sessions:
        s.status = _apply_standby_status(s.status, s.title)


def _apply_standby_status(status: str, title: str) -> str:
    """config-* sessions (Max's standing desk sessions: config-LEAD,
    config-MCPs, config-skills, config-claude.md, config-hooks) are
    always-idle infrastructure that sits there by design, not a one-off
    task that finished and is waiting on him specifically. Lumping them
    into READY pollutes the "needs you" signal with sessions that don't
    actually need him (Max, 2026-08-18: "anything with config- should go
    to STANDBY not READY as a canonical customization for me"). Only
    "done" is affected: working (someone's actively asking it something)
    and needs_approval stay exactly as urgent as they'd otherwise be."""
    if status == "done" and title.startswith("config-"):
        return "standby"
    return status


def determine_status(session_id: str, last_assistant_time: float,
                     display_title: str = "", transcript_path: str = "") -> str:
    """Two states actually matter: needs_approval (blocking on you, set by
    the PermissionRequest hook the instant it fires) and everything else.
    A prior version tried to also distinguish working/waiting/idle/background
    via a four-tier fallback stack with 5-minute decay timers reconstructed
    from polled files. A real incident held a closed session "working" for
    4.5 hours on a stale hook (EBC-Shell, 2026-06-04), and Max's own read
    was that none of those distinctions told him anything he'd act on.
    Collapsed to what's actually reliable: is Claude mid-turn right now
    (hook "thinking", still gated by the staleness guard below), is it
    blocked on you, or is it just done, reported as elapsed time by the
    caller (see format_ago on s.last_activity) rather than bucketed here."""
    alive = _is_session_alive(session_id)
    if not alive:
        return "closed"

    def _is_streaming_or_background() -> bool:
        if not transcript_path:
            return False
        # Hook says idle, but if the transcript itself is being appended to,
        # the model is streaming: slash commands and a few other paths can
        # miss UserPromptSubmit so the hook never flips.
        try:
            if time.time() - os.stat(transcript_path).st_mtime < 5:
                return True
        except OSError:
            pass
        return count_background_activity(transcript_path) > 0

    # Tier 1: hook state files (real-time, event-driven)
    hook = read_hook_state(session_id)
    if hook:
        hook_state = hook.get("state", "")
        if hook_state == "thinking":
            if not _thinking_is_stale(session_id, hook, transcript_path):
                return "working"
            mlog("DIVERGE", "stale_thinking", sid=session_id[:12],
                 hook_ts=hook.get("timestamp", ""),
                 entered=hook.get("state_entered_at", ""))
            return "done"
        if hook_state == "approval":
            return "needs_approval"
        if hook_state in ("exited", "idle"):
            # "exited" here is stale: a subagent's SessionEnd can write it to
            # the parent's state file while the parent is still running, and
            # we already know `alive` is True. Either way, fall through to
            # the transcript to see if something's actually still moving.
            return "working" if _is_streaming_or_background() else "done"

    # Tier 2: signal files (legacy)
    if SIGNALS_DIR.exists():
        signal_file = SIGNALS_DIR / session_id
        if signal_file.exists():
            try:
                s = signal_file.read_text().strip()
                return {"working": "working",
                        "permission": "needs_approval"}.get(s, "done")
            except OSError:
                pass

    # Tier 3: time-based heuristic (fallback, no hook file at all)
    if last_assistant_time > 0 and time.time() - last_assistant_time < 30:
        return "working"
    return "working" if _is_streaming_or_background() else "done"


# ── Session identity resolver (single chokepoint + self-audit) ──────────────────
# A transcript is a parentUuid TREE, not a line, and sessionId is conversation-
# grain (reused across every --resume), so identity must be RESOLVED, not read.
# resolve_session() is the ONE place that turns the desynced sources (pid files,
# hook state, transcript) into a single identity per running instance, keyed by a
# durable surrogate instance_id = "{sid}#{startedAt_ms}". Every renderer goes
# through it. audit_identities() asserts invariants each refresh and logs any
# violation (with the raw snapshot) as a ready-made test fixture, so correctness
# ratchets instead of being declared. Phase 1: files-only (pid files + hook +
# transcript). Phase 2 adds the persisted per-instance record; Phase 3 adds
# `claude agents --json` as liveness tier 0.

INSTANCE_SEP = "#"


def base_sid(key: str) -> str:
    """Normalize any row key (bare sid, legacy 'sid@pid', new 'sid#startedms')
    back to the bare conversation sid. Idempotent on a bare sid."""
    return key.split(INSTANCE_SEP, 1)[0].split("@", 1)[0]


IGNORE_MARKER = "&ignore"


def disambiguate_titles(flat: list[Session]) -> None:
    """Mutate titles in place so two visible rows never read identically.
    Pass 1 appends ·sid8 on a name collision. Pass 2 catches the sibling case
    (same conversation resumed in two pids: identical title AND identical sid8)
    by appending the pid/startedAt from the carrier key.

    Suffixes use only the · separator: _group_key() treats '@' as the explicit
    bugs@disclosey group sigil, so a literal '@pid' in a mutated title would
    re-key the sibling to a different group than its peers, break the
    contiguous-group invariant the renderer relies on, and DuplicateKey on the
    second '__group__config' header (observed live 2026-06-16, b9bb8e2d twins).
    """
    def _counts() -> dict[str, int]:
        c: dict[str, int] = {}
        for s in flat:
            if s.title:
                c[s.title] = c.get(s.title, 0) + 1
        return c
    tc = _counts()
    for s in flat:
        if s.title and tc.get(s.title, 0) > 1:
            s.title = f"{s.title} ·{base_sid(s.session_id)[:8]}"
    tc = _counts()
    for s in flat:
        if s.title and tc.get(s.title, 0) > 1:
            bare = base_sid(s.session_id)
            suffix = s.session_id[len(bare):].lstrip("@#")
            if suffix:
                s.title = f"{s.title}·{suffix}"


def filter_ignored(sessions: list[Session]) -> list[Session]:
    """Drop sessions whose name contains "&ignore" (case-insensitive) from
    monitoring entirely, along with their PID-siblings and subagents (keyed on
    the conversation sid). An explicit opt-out: it beats pinning. Naming a
    session with "&ignore" signals the monitor not to track it; remove the
    marker to track it again."""
    ignored_bases = {
        base_sid(s.session_id) for s in sessions
        if IGNORE_MARKER in (s.title or "").lower()
    }
    if not ignored_bases:
        return sessions
    return [
        s for s in sessions
        if base_sid(s.session_id) not in ignored_bases
        and base_sid(s.parent_id) not in ignored_bases
    ]


@dataclass(frozen=True)
class ResolvedIdentity:
    key: str            # unique row key (carrier): instance_id live, sid/legacy dead
    sid: str            # bare conversation id (for file lookups)
    instance_id: str    # durable surrogate "{sid}#{startedAt_ms}"
    pid: int | None
    started_ms: int
    title: str
    title_source: str   # known provenance, never empty for a resolved row
    status: str
    alive: bool
    cwd: str
    origin: str         # live | backfilled | reconstructed
    source: str         # pidfile | hook | transcript


def _started_ms_for(sid: str, pid: int | None) -> int:
    """Launch time in ms from the pid file (the stable half of the surrogate).
    0 when unknown (dead session, recycled/absent pid file, or sid mismatch)."""
    if pid is None:
        return 0
    try:
        data = json.loads((SESSIONS_DIR / f"{pid}.json").read_text())
        if data.get("sessionId") == sid:
            return int(data.get("startedAt", 0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return 0


def _resolve_title(session_id: str, data: dict, idx: dict, path: str,
                   hook: dict | None) -> tuple[str, str]:
    """Title + its provenance. Mirrors the precedence in build_session: live
    sessions let a user /rename (transcript custom_title) win; an EXITED session
    trusts its settled hook title over a transcript-tree straggler."""
    hook_title = hook.get("title", "") if hook else ""
    hook_exited = bool(hook) and hook.get("state") == "exited"
    candidates: list[tuple[str, str]]
    if hook_exited and hook_title:
        candidates = [(hook_title, "hook"), (data["custom_title"], "custom_title")]
    else:
        candidates = [(data["custom_title"], "custom_title"), (hook_title, "hook")]
    candidates += [
        (idx.get("summary", ""), "summary"),
        (read_session_memory_title(path), "memory"),
        (idx.get("firstPrompt", "")[:60], "firstPrompt"),
        (Path(data["cwd"]).name, "cwd"),
        (session_id[:8], "sid"),
    ]
    for value, source in candidates:
        if value:
            return value, source
    return session_id[:8], "sid"


def resolve_session(session_id: str, data: dict, idx: dict,
                    path: str) -> ResolvedIdentity:
    """The one chokepoint. Given a sid and its transcript scan + index, resolve
    the single identity (key, title, status, liveness, durable instance_id) from
    all sources under one precedence. Reads cached maps (pid map, hook cache);
    one small pid-file read for startedAt. Renderers must route through this."""
    hook = read_hook_state(session_id)
    alive = _is_session_alive(session_id)
    pid = _pid_map.get(session_id)
    started_ms = _started_ms_for(session_id, pid)
    title, title_source = _resolve_title(session_id, data, idx, path, hook)
    status = determine_status(session_id, data["last_assistant_time"], title, path)
    instance_id = f"{session_id}{INSTANCE_SEP}{started_ms}"

    if alive:
        key = instance_id
        origin = "live" if started_ms else "backfilled"
    elif pid is not None:
        key = f"{session_id}@{pid}"  # legacy reconstructed grain
        origin = "reconstructed"
    else:
        key = session_id
        origin = "reconstructed"
    source = "hook" if hook else ("pidfile" if pid is not None else "transcript")

    return ResolvedIdentity(
        key=key, sid=session_id, instance_id=instance_id, pid=pid,
        started_ms=started_ms, title=title, title_source=title_source,
        status=status, alive=alive, cwd=data.get("cwd", ""),
        origin=origin, source=source,
    )


def audit_identities(identities: list[ResolvedIdentity]) -> list[dict]:
    """Self-audit the resolved set against the invariants. Returns the list of
    violations and logs each (with snapshot) to monitor.log as a ready-made test
    fixture. The invariant set only grows; correctness ratchets."""
    violations: list[dict] = []

    def flag(kind: str, **details):
        violations.append({"kind": kind, **details})

    seen_keys: dict[str, ResolvedIdentity] = {}
    seen_iids: dict[str, str] = {}
    for r in identities:
        # 1. No two rows share a key.
        if r.key in seen_keys:
            flag("dup_key", key=r.key, sid=r.sid,
                 other_sid=seen_keys[r.key].sid)
        seen_keys[r.key] = r
        # 2. Key normalizes to exactly this row's sid.
        if base_sid(r.key) != r.sid:
            flag("key_sid_mismatch", key=r.key, sid=r.sid,
                 normalized=base_sid(r.key))
        # 3. No two LIVE rows share an instance_id.
        if r.alive:
            if r.instance_id in seen_iids and seen_iids[r.instance_id] != r.key:
                flag("dup_instance_id", instance_id=r.instance_id,
                     key=r.key, other_key=seen_iids[r.instance_id])
            seen_iids[r.instance_id] = r.key
        # 4. Every live row has a known title source (never empty/guessed).
        if r.alive and not r.title_source:
            flag("title_source_unknown", key=r.key, sid=r.sid, title=r.title)

    if violations:
        try:
            from monitor_log import log as mlog
            for v in violations:
                mlog("invariant", v["kind"], **{k: x for k, x in v.items()
                                                if k != "kind"})
        except Exception:
            pass
    return violations


# ── Formatting ────────────────────────────────────────────────────────────────


def format_model(model: str) -> str:
    for k, v in {
        "claude-opus-4-6": "Opus 4.6", "claude-sonnet-4-6": "Sonnet 4.6",
        "claude-haiku-4-5": "Haiku 4.5", "claude-sonnet-4-5": "Sonnet 4.5",
    }.items():
        if k in model:
            return v
    return model.replace("claude-", "").title()[:12] if model else "—"


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def format_ago(ts: float) -> str:
    elapsed = time.time() - ts
    if elapsed < 60:
        return f"{int(elapsed)}s"
    elif elapsed < 3600:
        return f"{int(elapsed / 60)}m"
    elif elapsed < 86400:
        return f"{int(elapsed / 3600)}h"
    return f"{int(elapsed / 86400)}d"


def format_duration(created: float, last_activity: float) -> str:
    if created <= 0:
        return "—"
    dur = last_activity - created
    if dur < 60:
        return f"{int(dur)}s"
    elif dur < 3600:
        return f"{int(dur / 60)}m"
    elif dur < 86400:
        h = int(dur / 3600)
        m = int((dur % 3600) / 60)
        return f"{h}h{m:02d}m"
    return f"{int(dur / 86400)}d"


def format_context_bar(pct: int, width: int = 10, is_estimate: bool = False) -> str:
    """Render context usage bar. pct = % of context USED (higher = worse).
    Estimates render dim with '?' — the token-count guess can be off by 5×
    when the model's window isn't known, so a number would mislead."""
    if is_estimate:
        return f"[dim]{'░' * width}   ?[/]"
    filled = round(pct / 100 * width)
    empty = width - filled
    if pct < 25:
        color = "bright_green"
    elif pct < 50:
        color = "green"
    elif pct < 75:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{'█' * filled}[/][dim]{'░' * empty}[/] {pct}%"


def format_compactions(count: int) -> str:
    if count == 0:
        return "[dim]—[/]"
    stars = "✻" * min(count, 5)
    if count > 5:
        stars += f"+{count - 5}"
    if count <= 2:
        color = "green"
    elif count == 3:
        color = "yellow"
    elif count == 4:
        color = "dark_orange"
    else:
        color = "red"
    return f"[{color}]{stars}[/]"


def format_cost(cost: float) -> str:
    if cost > 0:
        return f"${cost:.2f}"
    return "[dim]—[/]"


def _to_gerund(verb: str) -> str:
    """Convert a base verb to gerund form."""
    verb = verb.lower()
    if verb.endswith("ing"):
        return verb.capitalize()
    if verb.endswith("e") and not verb.endswith("ee"):
        return (verb[:-1] + "ing").capitalize()
    if re.match(r'.*[^aeiou][aeiou][^aeiouwxy]$', verb) and len(verb) <= 5:
        return (verb + verb[-1] + "ing").capitalize()
    return (verb + "ing").capitalize()


def _gerund_from_tool(name: str, inp: dict) -> str:
    """Generate a gerund phrase from a tool call."""
    # MCP tools: mcp__claude_ai_ServiceName__action_name
    if name.startswith("mcp__"):
        stripped = re.sub(r'^mcp__claude_ai_', '', name)
        if "__" in stripped:
            service_raw, action_raw = stripped.rsplit("__", 1)
        else:
            service_raw, action_raw = stripped, ""

        service = MCP_SERVICE_NAMES.get(service_raw, service_raw.replace("_", " "))

        # Strip common prefixes from action (e.g., whoop_ from whoop_get_recovery)
        action_clean = action_raw
        for prefix in ("whoop_",):
            if action_clean.startswith(prefix):
                action_clean = action_clean[len(prefix):]

        gerund = None
        remainder = ""
        for key, val in MCP_ACTION_GERUNDS.items():
            if action_clean.startswith(key):
                gerund = val
                remainder = action_clean[len(key):].strip("_").replace("_", " ")
                break

        if not gerund:
            words = action_clean.split("_")
            gerund = _to_gerund(words[0])
            remainder = " ".join(words[1:])

        # Drop remainder if it just repeats the service name (singular/plural)
        if remainder:
            svc_lower = service.lower().rstrip("s")
            rem_lower = remainder.lower().rstrip("s")
            if rem_lower == svc_lower or rem_lower in svc_lower:
                remainder = ""

        parts = [service]
        if remainder:
            parts.append(remainder)
        return f"{gerund} {' '.join(parts)}".strip()

    # Bash: use description if available, else command-based gerund
    if name == "Bash":
        desc = inp.get("description", "")
        if desc:
            return desc[:50]
        cmd = inp.get("command", "")
        cmd_word = cmd.split()[0] if cmd else ""
        return BASH_CMD_GERUNDS.get(cmd_word, "Running command")

    # Read/Edit/Write: include filename
    if name in ("Read", "Write", "Edit"):
        gerund = {"Read": "Reading", "Write": "Writing", "Edit": "Editing"}[name]
        fp = inp.get("file_path", "")
        filename = fp.rsplit("/", 1)[-1] if fp else ""
        return f"{gerund} {filename}" if filename else gerund

    # Grep: include search pattern
    if name == "Grep":
        pattern = inp.get("pattern", "")
        return f"Searching for '{pattern}'" if pattern else "Searching codebase"

    # Other known tools
    known = {
        "Glob": "Finding files", "WebSearch": "Searching web",
        "WebFetch": "Fetching page", "Agent": "Running subagent",
        "ToolSearch": "Loading tools", "AskUserQuestion": "Asking user",
    }
    return known.get(name, f"Using {name}")


def _gerund_from_text(text: str) -> str | None:
    """Try to extract a gerund from assistant text using patterns."""
    text = text.strip()
    for pattern, verb_group in TEXT_GERUND_PATTERNS:
        m = re.search(pattern, text)
        if m:
            if verb_group is None:
                # Already a gerund or "I'm Xing" — just clean up
                return m.group(0).rstrip(".,;:—")[:50]
            else:
                verb = m.group(verb_group)
                rest = m.group(verb_group + 1).split(".")[0].split(",")[0].split(" — ")[0].strip()
                return f"{_to_gerund(verb)} {rest}".strip()[:50]
    return None


def _to_past_tense(gerund_phrase: str) -> str:
    """Convert a gerund phrase to past tense: 'Reading config' → 'Read config'."""
    first_word = gerund_phrase.split()[0] if gerund_phrase else ""
    past = GERUND_TO_PAST.get(first_word)
    if past:
        rest = gerund_phrase[len(first_word):]
        return f"{past}{rest}"
    # Fallback: strip -ing, add -ed (rough but better than nothing)
    if first_word.endswith("ing"):
        base = first_word[:-3]
        return f"{base}ed{gerund_phrase[len(first_word):]}"
    return gerund_phrase


def generate_activity(s: Session) -> str:
    """Generate a status-aware activity description.

    Working → gerund:     "Editing claude_monitor.py"
    Approval → prompt:    "Awaiting approval"
    Done → past tense:    "Edited claude_monitor.py"
    """
    if s.background_count > 0:
        n = s.background_count
        return f"{n} agent{'s' if n != 1 else ''} running"

    # Tier 1: hook state (real-time tool + target)
    hook = read_hook_state(s.session_id)
    gerund = ""
    if hook and hook.get("tool"):
        tool = hook["tool"]
        target = hook.get("tool_target", "")
        inp = {"file_path": target} if target and not target.startswith(("http", "/", "git ", "npm ")) else {"command": target}
        gerund = _gerund_from_tool(tool, inp)

    # Tier 2: transcript-derived last_tool
    if not gerund and s.last_tool:
        gerund = _gerund_from_tool(s.last_tool, s.last_tool_input)
    if not gerund and s.last_assistant_text:
        gerund = _gerund_from_text(s.last_assistant_text) or ""

    # Fallback: truncate assistant text as summary
    if not gerund and s.last_assistant_text:
        text = s.last_assistant_text.strip()
        # Take first sentence or first N chars
        for sep in (".", "!", "?", "\n"):
            idx = text.find(sep)
            if 0 < idx < 60:
                text = text[:idx]
                break
        gerund = text[:50]

    if not gerund:
        return ""

    # Apply status-based transformation
    if s.status == "needs_approval":
        return "Awaiting approval"
    elif s.status in ("done", "closed", "standby"):
        return _to_past_tense(gerund)
    else:
        return gerund


def sort_sessions(sessions: list[Session], mode: SortMode) -> list[Session]:
    if mode == SortMode.ACTIVITY:
        out = sorted(sessions, key=lambda s: s.last_activity, reverse=True)
    elif mode == SortMode.STATUS:
        out = sorted(sessions, key=lambda s: (STATUS_PRIORITY.get(s.status, 9), -s.last_activity))
    elif mode == SortMode.ALPHA:
        out = sorted(sessions, key=lambda s: (s.title or s.session_id).lower())
    elif mode == SortMode.CONTEXT:
        out = sorted(sessions, key=lambda s: s.context_pct)
    elif mode == SortMode.TOKENS:
        out = sorted(sessions, key=lambda s: s.tokens_out, reverse=True)
    elif mode == SortMode.COST:
        out = sorted(sessions, key=lambda s: s.cost, reverse=True)
    else:
        out = list(sessions)
    # A '--' in the title is the group-lead marker (e.g. config--LEAD): float
    # those to the top in EVERY sort mode. Stable sort preserves the mode's
    # ordering among non-leads. Within-group order = this order, so a lead
    # row always sits directly under its group header.
    return sorted(out, key=lambda s: 0 if "--" in (s.title or "") else 1)


# ── Column rendering ──────────────────────────────────────────────────────────


def render_status_cell(status: str, spin_idx: int = 0, last_activity: float = 0.0,
                       seen_count: int = 0, dark: bool = True) -> str:
    icon, color = STATUS_DISPLAY.get(status, ("?", "white"))
    if status == "working":
        frame = SPINNER_FRAMES[spin_idx % len(SPINNER_FRAMES)]
        # The glyph still animates (motion is the "it's alive" cue) but in a
        # near-background gray, not a color: a moving colored dot on every
        # WORKING row out-shouted the READY rows that actually need
        # attention, the exact inversion the 2026-08-16 color rework was
        # meant to end (Max, 2026-08-18, twice: "the animating working ones
        # are popping", then "gray out the animating *, it's too
        # distracting"). See SPINNER_COLOR_*.
        glyph = SPINNER_COLOR_DARK if dark else SPINNER_COLOR_LIGHT
        return f"[{glyph}]{frame}[/] [{color}]WORKING[/]"
    if status == "done":
        color = READY_SEEN_COLOR if seen_count >= 1 else (READY_COLOR_DARK if dark else READY_COLOR_LIGHT)
        text = f"{icon} {format_ago(last_activity)}" if last_activity > 0 else icon
        # Checked once: still bold, just off the unseen color, so it still
        # pops a little (Max, 2026-08-16: "it can still be bold and ready
        # but not yellow"). Checked again with still no action: the badge
        # itself unbolds too, on top of the row title (Max, 2026-08-18:
        # "when something is checked twice without action please unbold
        # it in ready state").
        weight = "" if seen_count >= 2 else "bold "
        return f"[{weight}{color}]{text}[/]"
    return f"[{color}]{icon}[/]"


_GROUP_SPLIT = re.compile(r"[\s\-/_.:]+")


def _group_key(name: str) -> str:
    """Extract group key from a session name.

    - name with '@': part after @ is the explicit group (bugs@disclosey → disclosey)
    - otherwise: first word before space/-/_/./: is the implicit group
      (strategy-ideation → strategy)
    """
    if "@" in name:
        return name.rsplit("@", 1)[1].strip() or "ungrouped"
    parts = _GROUP_SPLIT.split(name.strip(), 1)
    return parts[0] if parts and parts[0] else "ungrouped"


def render_row(s: Session, visible_cols: list[str], spin_idx: int = 0,
               acked_ready: "dict[str, list] | set[str] | None" = None,
               dark: bool = True) -> list[str]:
    # READY read/unread (Max, 2026-08-16): jumping to a done session without
    # sending it anything acknowledges it. Still bold, still says READY,
    # just off the unseen color to mint (render_status_cell); the row title
    # unbolds too, the same "seen" distinction email gives read mail: the
    # color says what's true, the weight says whether you've already
    # looked. acked_ready is {sid: visit_count} (or a legacy set/list of
    # sids, each counting as one visit); a second visit without action
    # de-emphasizes the status badge a further notch (see
    # render_status_cell).
    seen_count = _effective_seen_count(acked_ready, s)
    seen_ready = seen_count >= 1
    cells = []
    for col in visible_cols:
        if col == "status":
            cells.append(render_status_cell(s.status, spin_idx, s.last_activity,
                                            seen_count=seen_count, dark=dark))
        elif col == "session":
            if s.is_subagent:
                cells.append(f"[dim]└─ {s.title}[/]")
            else:
                # Live sessions render bold so they pop against the dim
                # (archived/standby) and dark-gray (closed) rows. standby
                # isn't in INACTIVE_STATUSES (it's an alive desk session,
                # not a not-currently-running one, so hide_inactive_pins
                # shouldn't sweep it up), just checked here directly.
                unbold = s.status in INACTIVE_STATUSES or s.status == "standby" or seen_ready
                t = s.title if unbold else f"[bold]{s.title}[/bold]"
                if s.subagents:
                    t += f" [dim](+{len(s.subagents)})[/]"
                cells.append(t)
        elif col == "project":
            cells.append(s.project if not s.is_subagent else "")
        elif col == "model":
            cells.append(s.model)
        elif col == "context":
            cells.append("" if s.is_subagent else format_context_bar(
                s.context_pct, is_estimate=s.context_is_estimate))
        elif col == "compact":
            cells.append("" if s.is_subagent else format_compactions(s.compact_count))
        elif col == "tokens":
            cells.append(format_tokens(s.tokens_out))
        elif col == "cost":
            cells.append(format_cost(s.cost))
        elif col == "mcp":
            cells.append(str(s.mcp_calls) if s.mcp_calls else "[dim]—[/]")
        elif col == "msgs":
            cells.append(str(s.message_count) if not s.is_subagent else "")
        elif col == "duration":
            cells.append(format_duration(s.created, s.last_activity))
        elif col == "active":
            cells.append(format_ago(s.last_activity))
        elif col == "doing":
            activity = generate_activity(s)
            if activity:
                if len(activity) > DOING_MAX_WIDTH:
                    activity = activity[:DOING_MAX_WIDTH - 1] + "…"
                activity_escaped = _escape_markup(activity)
                if s.status in ("done", "standby"):
                    cells.append(f"[dim]{activity_escaped}[/]")
                elif s.status == "needs_approval":
                    cells.append(f"[yellow]{activity_escaped}[/]")
                else:
                    cells.append(activity_escaped)
            else:
                cells.append("[dim]—[/]")

    # Dim all cells for archived sessions
    if s.status == "archived":
        cells = [f"[dim]{c}[/]" if not c.startswith("[dim]") else c for c in cells]

    return cells


# ── Terminal focus ────────────────────────────────────────────────────────────


def _find_claude_pid(session: Session) -> int | None:
    """Find the Claude CLI PID for a session.

    Strategies (most reliable first):
    1. PID map (O(1) lookup, no I/O)
    2. lsof on the tasks directory for this session
    3. Match claude processes by session's transcript path
    """
    sid = session.session_id

    # Strategy 1: PID map
    _refresh_pid_map()
    pid = _pid_map.get(sid)
    if pid is not None:
        return pid

    # Strategy 2: find who has the tasks directory open
    tasks_path = str(TASKS_DIR / sid)
    try:
        result = subprocess.run(
            ["lsof", "+D", tasks_path],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and "claude" in parts[0].lower():
                try:
                    return int(parts[1])
                except ValueError:
                    continue
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Strategy 3: match claude processes from ps
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,comm"],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    claude_pids = []
    for line in result.stdout.strip().splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) >= 3:
            try:
                pid = int(parts[0])
                comm = parts[2].lower()
                if pid != my_pid and "claude" in comm and "monitor" not in comm \
                        and "helper" not in comm and "crashpad" not in comm \
                        and ".app" not in comm:
                    claude_pids.append(pid)
            except ValueError:
                continue

    # Check which claude process has files related to this session open
    for cpid in claude_pids:
        try:
            result = subprocess.run(
                ["lsof", "-p", str(cpid)],
                capture_output=True, text=True, timeout=2,
            )
            if sid in result.stdout:
                return cpid
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    return None



import monitor_log
from monitor_log import log as mlog


def _resolve_match_candidates(session: Session) -> list[str]:
    """Collect all candidate names for window title matching.

    Returns deduplicated candidates ordered by specificity:
    - ·{sid8} marker — hook writes this to terminal title, unique per session
    - /tmp/claude-name-{sid} — statusline's session_name
    - hook state title — session-memory or Haiku-generated
    - Session.status_name — cached session_name
    - Session.title — transcript-derived display title
    - cwd basename — last-resort fallback
    """
    sid8 = session.session_id[:8]
    candidates = [f"\u00b7{sid8}"]
    cwd_name = Path(session.cwd).name if session.cwd else ""
    # cwd basename is unsafe when it's the home dir (= username) — it
    # substring-matches any window title containing the username.
    if cwd_name and cwd_name == Path.home().name:
        cwd_name = ""
    hook = read_hook_state(session.session_id) or {}
    hook_title = hook.get("title", "")
    sl_name = _read_session_cache("name", session.session_id)
    # User-set titles are most reliable; statusline name can be stale after rename
    if hook.get("title_source") == "user":
        name_order = [hook_title, sl_name, session.status_name, session.title, cwd_name]
    else:
        name_order = [sl_name, hook_title, session.status_name, session.title, cwd_name]
    for name in name_order:
        if name and name not in candidates and name not in ("Claude Code", "~"):
            candidates.append(name)
    return candidates


def _raise_window_by_content(session: Session, then_text: str = "") -> bool:
    """Raise the terminal window/tab for a session — single JXA call.

    Checks both Ghostty and Terminal.app. Matches window titles against all
    candidates (·{sid8} marker first). Uses byName() lookup — z-order safe.
    If then_text is set, types it + Enter after raising.
    """
    candidates = _resolve_match_candidates(session)
    if not candidates:
        mlog("jump", "no_match_name", sid=session.session_id[:12])
        return False

    candidates_json = json.dumps(candidates)
    text_json = json.dumps(then_text)

    mlog("jump", "raise_attempt", candidates=candidates, then=then_text or None,
         sid=session.session_id[:12])

    jxa = f"""(() => {{
        const se = Application("System Events");
        const candidates = {candidates_json};
        const thenText = {text_json};

        for (const appName of ["Ghostty", "iTerm2", "Terminal"]) {{
            // App's own scripting bridge sees ALL windows across spaces
            // (System Events is scoped to current space)
            let app, allTitles;
            try {{
                app = Application(appName);
                allTitles = app.windows.name();
            }} catch(e) {{ continue; }}
            if (allTitles.length === 0) continue;

            let targetName = null;
            let matchedCand = null;

            // Phase 1: find ·sid8 match and best name-based match
            const sid8 = candidates[0];
            const sidMatches = allTitles.filter(t => t && t.includes(sid8));
            // Typing guard: a stale tab can keep an old ·sid8 marker while a
            // different live session runs inside it, so duplicate markers mean
            // the true host is unknowable from titles. Raising the wrong window
            // is recoverable; typing into it is not (a mistyped /rename
            // permanently renames the other conversation). Abort typing.
            if (thenText && sidMatches.length > 1) {{
                return "abort_multi_sid:" + sid8 + ":" + sidMatches.length;
            }}
            const sidWindow = sidMatches[0] || null;
            let nameWindow = null;
            let nameCand = null;
            // Extract the session name from a title like "✳ name ·sid8"
            // and do exact match against the candidate. This prevents
            // "strategy" from matching "strategy-patterns".
            function nameMatch(title, cand) {{
                // Strip emoji prefix and ·sid8 suffix to get bare name
                let bare = title.replace(/^[^\\w]*\\s*/, "").replace(/\\s*·[0-9a-f]{{8}}$/, "").trim();
                return bare === cand;
            }}
            let nameAmbiguous = false;
            for (let i = 1; i < candidates.length; i++) {{
                const ms = allTitles.filter(t => t && nameMatch(t, candidates[i]));
                if (ms.length > 0) {{
                    nameWindow = ms[0]; nameCand = candidates[i];
                    nameAmbiguous = ms.length > 1;
                    break;
                }}
            }}

            // Phase 2: resolve conflicts between sid marker and name match
            if (sidWindow && nameWindow && sidWindow === nameWindow) {{
                // Both point to the same window — ideal case
                targetName = sidWindow; matchedCand = sid8;
            }} else if (sidWindow && nameWindow && sidWindow !== nameWindow) {{
                // They diverge. If multiple windows share the name (e.g.,
                // after /branch + same /rename), the name match is the
                // ambiguous signal — trust the unique sid8. Otherwise the
                // sid marker is the stale one (old tab from a resume, or
                // Claude's auto-summary clobbered the marker).
                if (nameAmbiguous) {{
                    targetName = sidWindow; matchedCand = sid8 + "+name_ambiguous";
                }} else {{
                    targetName = nameWindow;
                    matchedCand = nameCand + "+sid_stale";
                }}
            }} else if (sidWindow) {{
                targetName = sidWindow; matchedCand = sid8;
            }} else if (nameWindow) {{
                targetName = nameWindow; matchedCand = nameCand;
            }}
            if (!targetName && appName === "Ghostty") {{
                // Phase 2.5 (Ghostty only, EVERY Space): walk Ghostty's OWN
                // window/tab tree. Phase 1 sees each window's ACTIVE tab
                // only, and the System Events walk below sees only the
                // CURRENT Space, so a background tab in a window on another
                // Space was invisible to both: jumping to it returned
                // no_match while its row sat right there in the monitor
                // (Max, 2026-08-27). Ghostty's own dictionary lists every
                // window and every tab regardless of Space. This phase only
                // DISCOVERS and surfaces the tab; the existing raise below
                // (AXRaise, else the Window-menu click that switches Spaces
                // natively) then brings its window forward.
                try {{
                    const gh = Application("Ghostty");
                    const sidHits = [], nameHits = [];
                    for (const w of gh.windows()) {{
                        let tabs;
                        try {{ tabs = w.tabs(); }} catch(e) {{ continue; }}
                        for (const tab of tabs) {{
                            let tt = "";
                            try {{ tt = tab.name() || ""; }} catch(e) {{ continue; }}
                            if (!tt) continue;
                            if (tt.includes(sid8)) sidHits.push([tab, tt]);
                            else if (candidates.slice(1).some(c => nameMatch(tt, c)))
                                nameHits.push([tab, tt]);
                        }}
                    }}
                    const hit = sidHits[0] || nameHits[0];
                    if (hit) {{
                        const [tab, tt] = hit;
                        // Same typing guard as every other path: type only
                        // when exactly one tab carries the sid marker.
                        if (thenText && sidHits.length !== 1)
                            return "abort_tab_type:" + tt + ":" + sidHits.length;
                        try {{ gh.selectTab(tab); }} catch(e) {{}}
                        delay(0.1);
                        targetName = tt;
                        matchedCand = sidHits.length ? sid8 : "ghostty_tab_name";
                    }}
                }} catch(e) {{}}
            }}
            if (!targetName) {{
                // Phase 3 (Ghostty only): a session can be a BACKGROUND TAB in a
                // tabbed window, whose title never appears in app.windows.name()
                // (that returns the active tab's title only). Walk the AX
                // tabGroups; click the matching radioButton to surface the tab.
                if (appName === "Ghostty") {{
                    try {{
                        const proc3 = se.processes.byName(appName);
                        // Collect all tab hits first so we can prove sid
                        // uniqueness before typing (mirrors Phase-1's
                        // abort_multi_sid guard for background tabs).
                        const sidHits = [], nameHits = [];
                        for (const w of proc3.windows()) {{
                            let tabs;
                            try {{ tabs = w.tabGroups[0].radioButtons(); }}
                            catch(e) {{ continue; }}
                            for (const tab of tabs) {{
                                const tt = tab.title();
                                if (!tt) continue;
                                if (tt.includes(sid8)) sidHits.push([w, tab, tt]);
                                else if (candidates.slice(1).some(c => nameMatch(tt, c)))
                                    nameHits.push([w, tab, tt]);
                            }}
                        }}
                        const hit = sidHits[0] || nameHits[0];
                        if (hit) {{
                            const [w, tab, tt] = hit;
                            if (thenText) {{
                                // Type only when exactly one tab carries the
                                // sid marker — name-only or duplicate-sid hits
                                // remain refused (wrong-tab typing is
                                // unrecoverable).
                                if (sidHits.length !== 1)
                                    return "abort_tab_type:" + tt + ":" + sidHits.length;
                            }}
                            try {{ proc3.frontmost = true; }} catch(e) {{}}
                            delay(0.05);
                            tab.click();
                            delay(0.1);
                            try {{ w.actions["AXRaise"].perform(); }} catch(e) {{}}
                            if (thenText) {{
                                delay(0.2);
                                se.keystroke(thenText); delay(0.1); se.keyCode(36);
                            }}
                            return "matched:tab:" + tt;
                        }}
                    }} catch(e) {{}}
                }}
                continue;
            }}

            // proc.frontmost (not app.activate()) — activate() can switch
            // spaces to wherever the app's key window is, then race the
            // menu click and snap back to the wrong desktop.
            const proc = se.processes.byName(appName);
            try {{ proc.frontmost = true; }} catch(e) {{}}
            delay(0.1);

            // Fast path: AXRaise if window is on the current space
            try {{
                const w = proc.windows.byName(targetName);
                w.actions["AXRaise"].perform();
                try {{ w.attributes["AXMain"].value = true; }} catch(e) {{}}
                if (thenText) {{ delay(0.15); se.keystroke(thenText); delay(0.1); se.keyCode(36); }}
                return "matched:" + matchedCand + ":" + targetName;
            }} catch(e) {{}}

            // Cross-space: click the Window menu item — macOS switches spaces natively
            try {{
                const menu = proc.menuBars[0].menuBarItems.byName("Window").menus[0];
                const items = menu.menuItems.name();
                // matchedCand may carry a +sid_stale suffix for
                // diagnostics — strip it for menu-item matching.
                const menuCand = matchedCand.split("+")[0];
                // Only consider the window-list section (after the last
                // separator) — earlier items are commands like
                // "Move to <display>" that would relocate the window.
                const lastSep = items.lastIndexOf(null);
                const windowItems = lastSep >= 0 ? items.slice(lastSep + 1) : items;
                const item = windowItems.find(n => n && n.includes(menuCand));
                if (item) {{
                    menu.menuItems.byName(item).click();
                    if (thenText) {{ delay(0.3); se.keystroke(thenText); delay(0.1); se.keyCode(36); }}
                    return "matched:" + matchedCand + ":menu:" + item;
                }}
            }} catch(e) {{
                return "menu_error:" + e.message;
            }}
            return "found_not_raised:" + matchedCand;
        }}
        return "no_match";
    }})()"""

    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", jxa],
            capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        mlog("jump", "jxa_result", result=out, sid=session.session_id[:12])
        if out.startswith("abort_multi_sid:"):
            mlog("DIVERGE", "send_multi_sid_marker",
                 sid=session.session_id[:12], result=out)
            return False
        if out.startswith("matched:"):
            matched_on = out.split(":", 2)[1] if ":" in out else ""
            if "+sid_stale" in matched_on:
                mlog("DIVERGE", "stale_sid_marker",
                     sid=session.session_id[:12], matched_on=matched_on,
                     full=out)
            # Post-jump verification: if the raised window's title contains
            # a ·{sid8} marker for a DIFFERENT session, we jumped wrong.
            target_sid8 = session.session_id[:8]
            m = re.search(r"\u00b7([0-9a-f]{8})", out)
            if m and m.group(1) != target_sid8:
                mlog("DIVERGE", "wrong_window",
                     target=target_sid8, raised=m.group(1),
                     matched_on=matched_on, full=out)
            return True
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        mlog("jump", "jxa_error", error=str(e), sid=session.session_id[:12])
    return False


# Read-time filtering (the refresh cycle intersects this against sessions
# still actually "done") makes a stale entry inert, not wrong, so this cap
# exists only to keep monitor-prefs.json from growing by one entry every
# time a session is ever jumped to while READY, across the tool's whole
# life on this machine, forever. Generous: nobody has this many sessions
# genuinely READY and unseen at once, so evicting the oldest never drops
# one that still matters.
_ACKED_READY_CAP = 200


# Every read-modify-write of monitor-prefs.json in this process goes through
# this lock. There are five writers (_mark_ready_seen from jump worker
# threads, next_cursor saves from the main thread and the poller,
# launch_count in on_mount, _save_view_state, and action_restart's seen-mark
# clear), and any two of them interleaving load->save lose one side's
# write: the whole file is rewritten from a snapshot every time. Two jump
# workers landing together (mashed Ctrl+Shift+N) or Shift+R's clear racing
# an in-flight jump were the concrete survivors after the 2026-08-16
# single-writer fix for acked_ready (advisor review, 2026-08-18). The cold
# --jump-next process is the only cross-process writer, and it only runs
# when no monitor is up, so an in-process lock covers the real cases.
_prefs_lock = threading.Lock()


def _update_prefs(mutate) -> None:
    """Atomic (in-process) read-modify-write: `mutate(prefs)` edits the dict
    in place and returns True to save, False to skip the write."""
    with _prefs_lock:
        prefs = load_prefs()
        if mutate(prefs):
            save_prefs(prefs)


def _normalize_acked_ready(raw) -> dict[str, list]:
    """acked_ready's on-disk shape has changed twice. Originally a plain
    list of seen session ids. Then (2026-08-18 am) {sid: visit_count}, to
    support a second de-emphasis tier for a session checked more than once
    without action (Max: "when something is checked twice without action
    please unbold it in ready state"). Now {sid: [visit_count,
    last_activity_at_mark]}: the activity stamp is what lets an ack expire
    (see _effective_seen_count) once the session does new work. Normalizes
    all three shapes so an older prefs file (or a test using an older
    literal) reads correctly: legacy entries get stamp 0.0, which never
    voids (nothing has last_activity < 0), matching their old semantics."""
    if not isinstance(raw, dict):
        return {sid: [1, 0.0] for sid in raw}
    out: dict[str, list] = {}
    for sid, v in raw.items():
        if isinstance(v, (list, tuple)):
            count = int(v[0]) if len(v) >= 1 else 1
            stamp = float(v[1]) if len(v) >= 2 else 0.0
            out[sid] = [count, stamp]
        else:
            out[sid] = [int(v), 0.0]
    return out


def _effective_seen_count(acked: "dict[str, list] | set[str] | None", s: "Session") -> int:
    """How many times this done session has been checked SINCE it last did
    any work. 0 means unseen (or not done). An ack whose stamp is older
    than the session's current last_activity is void: you jumped to it,
    then gave it work (or it finished something else), and it has now
    become done AGAIN, which is exactly "until it cycles through another
    state and becomes done again" from _mark_ready_seen's contract.

    Bug this closes (advisor review, 2026-08-18, introduced by #51): the
    refresh cycle's old prune-and-write-back was removed to fix a race,
    but it was also the only thing that ever expired an ack, so after that
    change a seen-mark survived done->working->done forever. Concretely:
    jump to READY session A, give it new work, A finishes 20 minutes
    later, and A renders as already-seen and Ctrl+Shift+N skips it: a
    freshly finished session you have never looked at was invisible to
    the whole needs-you flow until Shift+R. Comparing stamps at read time
    fixes it with no second writer, keeping the single-writer discipline."""
    if s.status != "done" or not acked or s.session_id not in acked:
        return 0
    if not isinstance(acked, dict):
        return 1  # legacy set/list shape: seen once, never expires
    count, stamp = acked[s.session_id]
    if s.last_activity > stamp:
        return 0
    return count


def _mark_ready_seen(session_id: str, status: str, last_activity: float = 0.0) -> None:
    """Read/unread for READY rows (Max, 2026-08-16): jumping to a session
    while it's done acknowledges it, so its row unbolds without touching
    its yellow status, until it cycles through another state and becomes
    done again. Called from every jump path (SessionMenu, double-click, the
    "n" key, and the headless --jump-next CLI) so all of them count as
    "you looked at it," not just one. Persisted rather than held in memory:
    --jump-next runs as its own short-lived process, so the running
    monitor can only learn about that jump by reading it back from disk.
    The single writer of acked_ready: the refresh cycle only ever reads and
    filters it in memory (see _refresh_compute), never writes it back,
    after an earlier version's write-back raced this function's own
    read-modify-write and clobbered a just-added seen mark (Max, 2026-08-16:
    "my 'marked as read' doesn't seem to stay that way").

    Increments rather than just setting a flag (2026-08-18): a session
    checked a second time without you acting on it de-emphasizes further
    (the status badge itself unbolds, on top of already being mint and
    the row title already being unbold from the first check). The count
    restarts at 1 whenever the session has been active since the previous
    mark, so "checked twice" never spans two separate done episodes.

    Eviction past the cap is least-recently-marked (pop-then-reinsert
    moves a re-marked key to the end), not first-ever-marked; the naive
    version could evict a still-done session you revisited yesterday
    while keeping long-dead sids (advisor review, 2026-08-18)."""
    if status != "done":
        return

    def mutate(prefs: dict) -> bool:
        acked = _normalize_acked_ready(prefs.get("acked_ready", {}))
        prev = acked.pop(session_id, None)
        if prev is not None and last_activity <= prev[1]:
            count = prev[0] + 1
        else:
            count = 1
        acked[session_id] = [count, last_activity]
        if len(acked) > _ACKED_READY_CAP:
            acked = dict(list(acked.items())[-_ACKED_READY_CAP:])
        prefs["acked_ready"] = acked
        return True

    _update_prefs(mutate)


def focus_terminal_session(session: Session) -> bool:
    """Find and activate the terminal window containing this session.

    Single JXA call checks both Ghostty and Terminal.app, matches against
    all candidates (·{sid8} marker first), and raises the window.
    """
    mlog("jump", "focus_start", sid=session.session_id[:12], title=session.title)
    return _raise_window_by_content(session)


def session_has_debrief(transcript_path: str) -> bool:
    """Check if /debrief was already run by scanning the tail of the transcript."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 50_000)
            f.seek(size - chunk)
            tail = f.read()
    except OSError:
        return False
    return b"/debrief" in tail or b'"skill":"debrief"' in tail


def _send_to_terminal_session(session: Session, text: str, return_to_monitor: bool = False) -> bool:
    """Send text to the session's terminal. If return_to_monitor is True,
    raise the monitor window back after typing."""
    ok = _raise_window_by_content(session, then_text=text)
    if ok and return_to_monitor:
        import threading
        def _bounce_back():
            time.sleep(0.5)
            _raise_monitor_window()
            mlog("jump", "bounce_back", sid=session.session_id[:12])
        threading.Thread(target=_bounce_back, daemon=True).start()
    return ok


def _raise_monitor_window() -> None:
    """Raise the Claude Monitor window via its ·MONITOR title marker.
    Uses Window menu click (works cross-space) without activate() to
    avoid raising all Ghostty windows."""
    script = '''(() => {
        const se = Application("System Events");
        for (const appName of ["Ghostty", "iTerm2", "Terminal"]) {
            let app, titles;
            try {
                app = Application(appName);
                titles = app.windows.name();
            } catch(e) { continue; }
            const match = titles.find(t => t && t.includes("·MONITOR"));
            if (!match) continue;

            const proc = se.processes.byName(appName);

            // Try Window menu click — works cross-space without activate()
            try {
                const menu = proc.menuBars[0].menuBarItems.byName("Window").menus[0];
                const items = menu.menuItems.name();
                const item = items.find(n => n && n.includes("·MONITOR"));
                if (item) { menu.menuItems.byName(item).click(); return "ok"; }
            } catch(e) {}

            // Fallback: AXRaise (same-space only)
            try {
                const w = proc.windows.byName(match);
                w.actions["AXRaise"].perform();
                try { w.attributes["AXMain"].value = true; } catch(e) {}
                return "ok";
            } catch(e) {}
        }
        return "no_raise";
    })()'''
    try:
        subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


# ── Layout save/restore ────────────────────────────────────────────────────────
# Max, 2026-08-23: "a command that saves which claudes are pinned, saves which
# claudes are in which windows and tabs, then pins all the claudes and
# restarts everything so that I can close and reopen ghostty and then
# regenerate where I left off."
#
# Snapshot shape (monitor-layout.json):
#   {"saved_at": float, "pinned_before": [sid...],
#    "windows": [{"frame": [x, y, w, h], "active": int,
#                 "tabs": [{"sid": "<full sid>" | null, "title": str}, ...]}]}
# A null sid is a tab with no Claude in it (a plain shell, or the monitor
# itself, see MONITOR_TAB_MARK); it is kept so tab ORDER survives.
#
# Reading is one System Events AX walk (the same one jump uses), so what it
# sees is exactly what jump can find. Writing uses Ghostty's own scripting
# dictionary (`new window`, `new tab in`, `select tab`) and AX position/size
# sets: no synthetic keystrokes anywhere, which matters because any injected
# key fires Claude Nest's push-to-talk (see resume_session).

MONITOR_TAB_MARK = "·MONITOR"  # the monitor's own tab title carries this
_SID8_RE = re.compile(r"·([0-9a-f]{8})\b")


def _snapshot_ghostty_layout() -> list[dict] | None:
    """Every Ghostty window, across every Space: its tabs in order (title,
    selected) from Ghostty's OWN scripting dictionary, and its frame from
    CoreGraphics' window list, joined by tab title (the tabs of one
    window are separate NSWindows sharing one frame, so any tab's title
    finds the frame). Returns None (not []) when the read failed.

    The first cut read everything through System Events AX, which lists
    only the windows on the CURRENT Space: on 2026-08-24, with stakes,
    it saved 4 of Max's 41 windows and reported success, twice. Ghostty's
    dictionary sees all 41 but has no frame property; CoreGraphics sees
    all 41 frames (kCGWindowListOptionAll, owner Ghostty, layer 0) but
    cannot enumerate tabs. Joined, they are complete: verified 41/41
    framed, and exact agreement with AX for the 10 AX could see. A frame
    of [0,0,0,0] means no CG entry matched (rare: a window whose every
    tab title is empty); the builder then leaves placement to Ghostty."""
    jxa = """(() => {
      ObjC.import("CoreGraphics");
      const cg = {};
      try {
        const arr = ObjC.castRefToObject(
          $.CGWindowListCopyWindowInfo($.kCGWindowListOptionAll, $.kCGNullWindowID));
        for (let i = 0; i < arr.count; i++) {
          const d = arr.objectAtIndex(i);
          if (ObjC.unwrap(d.objectForKey("kCGWindowOwnerName")) !== "Ghostty") continue;
          if (ObjC.unwrap(d.objectForKey("kCGWindowLayer")) !== 0) continue;
          const name = ObjC.unwrap(d.objectForKey("kCGWindowName")) || "";
          if (!name) continue;
          const b = ObjC.deepUnwrap(d.objectForKey("kCGWindowBounds"));
          if (!cg[name]) cg[name] = [b.X, b.Y, b.Width, b.Height];
        }
      } catch (e) { return "ERR:cg:" + e; }
      let g;
      try { g = Application("Ghostty"); g.windows(); } catch (e) { return "ERR:ghostty:" + e; }
      const out = [];
      for (const w of g.windows()) {
        let tabs = [];
        try { tabs = w.tabs().map(t => ({title: t.name(), active: !!t.selected()})); } catch (e) {}
        let name = "";
        try { name = w.name(); } catch (e) {}
        if (!tabs.length) tabs = [{title: name, active: true}];
        let frame = [0, 0, 0, 0];
        for (const t of tabs) { if (cg[t.title]) { frame = cg[t.title]; break; } }
        if (frame[2] === 0 && cg[name]) frame = cg[name];
        out.push({name: name, frame: frame, tabs: tabs});
      }
      return JSON.stringify(out);
    })()"""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", jxa],
                           capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError) as e:
        mlog("layout", "snapshot_failed", error=str(e))
        return None
    out = r.stdout.strip()
    if r.returncode != 0 or out.startswith("ERR:") or not out:
        mlog("layout", "snapshot_failed", rc=r.returncode, out=out[:120], err=r.stderr[:120])
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        mlog("layout", "snapshot_failed", out=out[:120])
        return None


def _layout_from_snapshot(raw_windows: list[dict], sessions: list[Session]) -> dict:
    """Resolve AX tab titles to full sids. The title carries only sid8; the
    session list maps it back to the full id. Keys are normalised through
    base_sid(): a conversation open in two pids is listed as 'uuid@pid'
    sibling rows, and saving that key would pin and restore a string that
    matches nothing once the pids are gone (review, 2026-08-23). Pure."""
    by_sid8: dict[str, str] = {}
    by_title: dict[str, set[str]] = {}
    for s in sessions:
        if s.is_subagent:
            continue
        sid = base_sid(s.session_id)
        by_sid8.setdefault(sid[:8], sid)
        by_title.setdefault(s.title, set()).add(sid)
    # Fallback for tabs with no sid8 marker: a tab in Claude Nest voice
    # mode is titled "◐ <name>" with no marker at all, so a marker-only
    # resolver saved the session Max was actually talking to as a plain
    # shell (live, 2026-08-24, with real stakes: he was about to close
    # Ghostty). Exact title match, accepted only when it names ONE
    # conversation after base_sid() folding; an ambiguous title stays
    # unresolved rather than guessing.
    def resolve_by_title(title: str) -> str | None:
        bare = re.sub(r"^[^\w]+", "", title).strip()
        sids = by_title.get(bare, set())
        return next(iter(sids)) if len(sids) == 1 else None

    windows = []
    for w in raw_windows:
        tabs = []
        active = None
        win_name = w.get("name", "") or ""
        for i, t in enumerate(w.get("tabs", [])):
            title = t.get("title", "") or ""
            if t.get("active"):
                active = i
            # Belt and braces: the window's own title always names the
            # selected tab, so use it if the radio value was unreadable.
            if active is None and win_name and title == win_name:
                active = i
            sid = None
            if MONITOR_TAB_MARK not in title:
                m = _SID8_RE.search(title)
                if m:
                    sid = by_sid8.get(m.group(1))
                if sid is None:
                    sid = resolve_by_title(title)
            tabs.append({"sid": sid, "title": title,
                         "monitor": MONITOR_TAB_MARK in title})
        windows.append({"frame": w.get("frame", [0, 0, 0, 0]), "active": active or 0, "tabs": tabs})
    return {"saved_at": time.time(), "windows": windows}


def save_layout(sessions: list[Session] | None = None) -> dict:
    """Snapshot the Ghostty layout, pin every live Claude in it (so none
    ages out of the monitor while Ghostty is closed), and persist both.

    `sessions`: pass the running app's own list from the Ctrl+L path. The
    first cut called parse_sessions() here on a worker thread, racing the
    refresh worker's parse_sessions() on the unlocked module _scan_cache;
    _save_scan_cache_to_disk() iterates that dict uncopied, so the race
    raised "dictionary changed size during iteration" and, with
    run_worker's exit_on_error default, took the whole monitor down
    (review, 2026-08-23). Only the CLI path, where no refresh worker
    exists, parses cold.

    Refuses to overwrite the last good snapshot when the AX read failed or
    saw no windows: an Accessibility-denied shell or a save after Ghostty
    already quit used to write `windows: []` and exit 0, destroying the
    snapshot the user was about to restore from (review, 2026-08-23).

    `pinned_before` is the pin set from BEFORE THE FIRST save in a chain,
    carried forward from the existing layout file: re-reading the pin file
    on a second save would record the first save's additions as "before",
    so --restore-pins could never roll back past the latest save."""
    pinned_now = load_pinned_sessions()
    if sessions is None:
        sessions = parse_sessions(include_archived=False, include_subagents=False,
                                  pinned=pinned_now)
    raw = _snapshot_ghostty_layout()
    if not raw:
        reason = "Ghostty layout could not be read" if raw is None else "no Ghostty windows"
        mlog("layout", "save_refused", reason=reason)
        return {"ok": False, "reason": reason,
                "summary": {"windows": 0, "claudes": 0, "newly_pinned": 0, "unresolved_tabs": 0}}
    layout = _layout_from_snapshot(raw, sessions)
    previous = load_layout()
    if previous and "pinned_before" in previous:
        layout["pinned_before"] = previous["pinned_before"]
    else:
        layout["pinned_before"] = sorted(pinned_now)
    sids = {t["sid"] for w in layout["windows"] for t in w["tabs"] if t["sid"]}
    save_pinned_sessions(pinned_now | sids)
    try:
        if previous:
            LAYOUT_PATH.with_suffix(".json.bak").write_text(json.dumps(previous, indent=2))
        LAYOUT_PATH.write_text(json.dumps(layout, indent=2))
    except OSError as e:
        mlog("layout", "save_write_failed", error=str(e))
        return {"ok": False, "reason": f"could not write {LAYOUT_PATH}", "summary": {
            "windows": len(layout["windows"]), "claudes": len(sids), "newly_pinned": 0,
            "unresolved_tabs": 0}}
    layout["ok"] = True
    layout["summary"] = {
        "previous_windows": len(previous["windows"]) if previous else None,
        "windows": len(layout["windows"]),
        "claudes": len(sids),
        "newly_pinned": len(sids - pinned_now),
        "unresolved_tabs": sum(1 for w in layout["windows"] for t in w["tabs"]
                               if not t["sid"] and not t["monitor"]),
    }
    mlog("layout", "saved", **layout["summary"])
    return layout


def load_layout() -> dict | None:
    try:
        return json.loads(LAYOUT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


LAYOUT_STAMP_PREFIX = "·layout-"


def _restore_plan(layout: dict, sessions: list[Session],
                  live_sids: set[str] | None = None,
                  visible_sid8s: set[str] | None = None,
                  monitor_running: bool = False,
                  compact: bool = True) -> tuple[list[dict], list[str], list[str]]:
    """Turn a saved layout into concrete launch steps. Pure. Returns
    (windows_to_build, missing_sids, skipped_live_sids).

    Skips and reports a session that is already alive or already visible
    in a terminal title: every interactive resume path refuses to spawn a
    duplicate (Claude Code's single-instance guard kicks it and leaves a
    dead tab), and a restore run twice, or run before Ghostty was actually
    quit, would have done exactly that for every tab (review, 2026-08-23).
    The monitor tab is likewise skipped when a monitor is already running.

    The restored active index is resolved AFTER dropping unrestorable
    tabs, by tracking which surviving tab carried the saved index; a
    plain clamp selected the wrong tab whenever a dropped tab sat before
    the active one (review, 2026-08-23)."""
    by_sid = {base_sid(s.session_id): s for s in sessions}
    live_sids = {base_sid(x) for x in (live_sids or set())}
    visible_sid8s = visible_sid8s or set()
    plan, missing, skipped_live = [], [], []
    # One conversation, one tab: a layout can hold the same sid twice (the
    # same session open in two terminals at save time). Resuming it twice
    # just makes Claude's single-instance guard kill one of them.
    planned_sids: set[str] = set()
    for w in layout.get("windows", []):
        tabs = []
        active_label = None
        saved_active = w.get("active", 0)
        for i, t in enumerate(w.get("tabs", [])):
            entry = None
            if t.get("monitor"):
                if monitor_running:
                    pass
                else:
                    entry = {"cmd": _ghostty_surface_command(str(_REPO_DIR), "claude-monitor"),
                             "label": "claude-monitor"}
            else:
                sid = t.get("sid")
                if not sid:
                    entry = {"cmd": "", "label": t.get("title", "")}  # plain shell
                else:
                    sid = base_sid(sid)
                    sess = by_sid.get(sid)
                    if sess is None or not (sess.transcript_path and Path(sess.transcript_path).exists()):
                        missing.append(sid)
                    elif sid in live_sids or sid[:8] in visible_sid8s:
                        skipped_live.append(sid)
                    elif sid in planned_sids:
                        pass  # already given a tab in this plan
                    else:
                        planned_sids.add(sid)
                        cmd, cwd = _resume_command_for(sess)
                        entry = {"cmd": _ghostty_surface_command(cwd, cmd), "label": sess.title}
            if entry is not None:
                tabs.append(entry)
                if i == saved_active:
                    active_label = len(tabs) - 1
        if tabs:
            if active_label is None:
                active_label = min(saved_active, len(tabs) - 1)
            # Unique title stamp on the first tab: how _ghostty_build_window
            # finds THIS window in the AX tree to frame it. Claude or the
            # prompt overwrites it moments later, which is fine; the frame
            # is applied before that. "stamp" is kept on the window so the
            # builder and a test can read it back.
            plan.append({"frame": w.get("frame", [0, 0, 0, 0]), "active": active_label,
                         "tabs": tabs})
    if compact:
        plan = _compact_plan(plan)
    _stamp_plan(plan)
    return plan, missing, skipped_live


LAYOUT_TABS_PER_WINDOW = 8


def _compact_plan(plan: list[dict], per_window: int = LAYOUT_TABS_PER_WINDOW) -> list[dict]:
    """Fold one-tab windows into a few grouped, tabbed windows.

    macOS cannot place a window on a Space other than the current one, so
    a literal rebuild of a layout spread over several Spaces lands every
    window on ONE Space. On 2026-08-24 that meant 41 overlapping windows
    and 44 Claude TUIs at once: unusable, and Ghostty went down minutes
    later taking every session with it. Since the monitor's own jump
    handles background tabs, tabs cost nothing to reach and stack cleanly.

    Windows that already held two or more tabs were grouped on purpose and
    are kept exactly, frame and all. Single-tab windows are regrouped by
    session-name prefix (the same _group_key the table groups by) into
    windows of at most `per_window` tabs, positioned by Ghostty."""
    kept = [w for w in plan if len(w["tabs"]) > 1]
    singles = [w for w in plan if len(w["tabs"]) == 1]
    groups: dict[str, list[dict]] = {}
    for w in singles:
        groups.setdefault(_group_key(w["tabs"][0]["label"]), []).append(w)
    # A group of one is not a group: those would each still take a whole
    # window, which is the problem we are solving. Pool them.
    loners = [w for k, wins in groups.items() if len(wins) < 2 for w in wins]
    real = {k: v for k, v in groups.items() if len(v) > 1}
    if loners:
        real["misc"] = loners
    folded = []
    for key in sorted(real):
        wins = real[key]
        for i in range(0, len(wins), per_window):
            chunk = wins[i:i + per_window]
            folded.append({"frame": [0, 0, 0, 0], "active": 0,
                           "tabs": [w["tabs"][0] for w in chunk], "group": key})
    return kept + folded


def _stamp_plan(plan: list[dict]) -> None:
    """Give each window's first tab a unique OSC title stamp so the builder
    can find that window in the AX tree to frame it."""
    for i, w in enumerate(plan):
        stamp = f"{LAYOUT_STAMP_PREFIX}{i + 1}-{int(time.time() * 1000) % 100000}"
        w["tabs"][0]["cmd"] = _restamp_surface_command(w["tabs"][0]["cmd"], stamp)
        w["stamp"] = stamp


def _restamp_surface_command(cmd: str, stamp: str) -> str:
    """Prefix an existing surface command (or an empty placeholder) with a
    title stamp. Pure string work; the plan builds commands before it
    knows which one leads a window."""
    if not cmd:
        return _ghostty_surface_command(str(Path.home()), "", title_stamp=stamp)
    # cmd is "/bin/zsh -ic '<inner>'": re-wrap with the stamp in front.
    prefix = "/bin/zsh -ic "
    if cmd.startswith(prefix):
        inner = shlex.split(cmd[len(prefix):])[0]
        inner = f"printf '\\033]0;{stamp}\\007' && {inner}"
        return f"{prefix}{shlex.quote(inner)}"
    return cmd


def _ghostty_build_window(window: dict) -> str:
    """Create one window with its tabs through Ghostty's own dictionary
    (no keystrokes), then set its frame via AX and select the saved tab.
    Returns "ok", "ok_unframed" (built but the frame could not be applied),
    or "failed".

    Ghostty has no frame property in its dictionary, so position/size go
    through System Events, a property set, not an input event. The AX
    window to frame is found by the unique title STAMP the plan put on
    its first tab's command (see _restore_plan). Two earlier schemes
    failed live (2026-08-23): `proc.windows()[0]` was the frontmost
    pre-existing window (Max's monitor), and a before/after diff of AX
    window names or frames collided because fresh windows share one
    placeholder title and one default frame. A frame that could not be
    applied is reported, never swallowed."""
    tabs = window["tabs"]
    first, rest = tabs[0], tabs[1:]
    x, y, w, h = window["frame"]
    stamp = window.get("stamp", "")
    jxa = f"""(() => {{
      const g = Application("Ghostty");
      const se = Application("System Events");
      const mk = (cmd) => cmd ? {{withConfiguration: {{command: cmd}}}} : {{}};
      const win = g.newWindow(mk({json.dumps(first["cmd"])}));
      // All tabs FIRST, then frame: adding a tab makes Ghostty re-lay the
      // window out (tab bar appears, width grows), which undid a frame
      // set before the tabs existed (live probe, 2026-08-23).
      for (const cmd of {json.dumps([t["cmd"] for t in rest])}) {{
        delay(0.35);
        g.newTab(Object.assign({{in: win}}, mk(cmd)));
      }}
      delay(0.3);
      let framed = !({w} > 0 && {h} > 0);  // no saved frame: nothing to apply
      const stamp = {json.dumps(stamp)};
      if (stamp && {w} > 0 && {h} > 0) {{
        // Find the window by ANY tab carrying the stamp (the active tab
        // may already be a later one), polling briefly for the shell to
        // emit its title.
        const carries = (aw) => {{
          try {{ if (aw.name() === stamp) return true; }} catch (e) {{}}
          try {{ return aw.tabGroups[0].radioButtons().some(t => t.title() === stamp); }}
          catch (e) {{ return false; }}
        }};
        let hit = null;
        for (let i = 0; i < 20 && !hit; i++) {{
          try {{ hit = se.processes.byName("Ghostty").windows().find(carries); }} catch (e) {{}}
          if (!hit) delay(0.1);
        }}
        // Set, then VERIFY, retrying a few times: Ghostty re-lays the
        // window out for a moment after the last tab is added, and a
        // single blind set in that window was overridden (widths came
        // back 852/1252 for 660/640 requested; every width is accepted
        // once the window has settled, so this is timing, not a minimum).
        for (let i = 0; hit && i < 8 && !framed; i++) {{
          try {{
            hit.position = [{x}, {y}];
            hit.size = [{w}, {h}];
            delay(0.15);
            const p = hit.position(), sz = hit.size();
            framed = (p[0] === {x} && p[1] === {y} && sz[0] === {w} && sz[1] === {h});
          }} catch (e) {{}}
        }}
      }}
      try {{
        const t = win.tabs()[{window["active"]}];
        if (t) g.selectTab(t);
      }} catch (e) {{}}
      return framed ? "ok" : "ok_unframed";
    }})()"""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", jxa],
                           capture_output=True, text=True, timeout=60)
        out = r.stdout.strip()
        if r.returncode == 0 and out in ("ok", "ok_unframed"):
            return out
        mlog("layout", "build_window_failed", rc=r.returncode, err=r.stderr[:200])
        return "failed"
    except (subprocess.SubprocessError, OSError) as e:
        mlog("layout", "build_window_failed", error=str(e))
        return "failed"


def restore_layout(restore_pins: bool = False, compact: bool = True,
                   dry_run: bool = False) -> dict:
    """Rebuild the saved layout. Windows are created in saved order. Live
    or already-visible sessions are skipped (never duplicated) and
    reported. `restore_pins` puts the pin list back to what it was before
    the first save; the default leaves everything pinned (safer: nothing
    ages out until Max unpins it himself)."""
    layout = load_layout()
    if not layout:
        return {"ok": False, "reason": "no saved layout"}
    sessions = parse_sessions(include_archived=True, include_subagents=False,
                              pinned=load_pinned_sessions())
    live = {base_sid(s.session_id) for s in sessions if _is_session_alive(s.session_id)}
    visible = _snapshot_window_sids()
    monitor_running = _a_monitor_is_running()
    plan, missing, skipped_live = _restore_plan(
        layout, sessions, live_sids=live, visible_sid8s=visible,
        monitor_running=monitor_running, compact=compact)
    if dry_run:
        return {"ok": True, "dry_run": True, "windows_planned": len(plan),
                "tabs_planned": sum(len(w["tabs"]) for w in plan),
                "plan": [{"group": w.get("group", ""), "frame": w["frame"],
                          "tabs": [t["label"] for t in w["tabs"]]} for w in plan],
                "missing": missing, "skipped_live": skipped_live}
    built = unframed = 0
    for window in plan:
        outcome = _ghostty_build_window(window)
        if outcome != "failed":
            built += 1
        if outcome == "ok_unframed":
            unframed += 1
        time.sleep(0.5)
    if restore_pins and "pinned_before" in layout:
        save_pinned_sessions(set(layout["pinned_before"]))
    result = {"ok": built == len(plan), "windows_built": built, "windows_planned": len(plan),
              "windows_unframed": unframed, "missing": missing, "skipped_live": skipped_live}
    mlog("layout", "restored", **result)
    return result


def _frontmost_terminal_title() -> str:
    """Return the frontmost Ghostty/iTerm2/Terminal window title (or '')."""
    jxa = """(() => {
        const se = Application("System Events");
        for (const appName of ["Ghostty", "iTerm2", "Terminal"]) {
            try {
                const proc = se.processes.byName(appName);
                if (proc.frontmost()) return proc.windows[0].name();
            } catch(e) {}
        }
        return "";
    })()"""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", jxa],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _close_terminal_tab(session: Session) -> bool:
    """Close the terminal tab for a session by sending 'exit' + Enter."""
    return _raise_window_by_content(session, then_text="exit")


DEBRIEF_DONE_PREFIX = Path("/tmp")
DEBRIEF_DONE_PATTERN = "claude-debrief-done-*"


def _poll_debrief_done_signals(sessions: list[Session]) -> list[str]:
    """Check for debrief-done signal files and close matching terminal tabs.

    Returns list of session IDs that were cleaned up.
    """
    cleaned = []
    for signal_file in DEBRIEF_DONE_PREFIX.glob(DEBRIEF_DONE_PATTERN):
        sid = signal_file.name.removeprefix("claude-debrief-done-")
        if not sid:
            continue

        # Find the matching session to get its name for tab closing
        session = next((s for s in sessions if s.session_id == sid), None)
        mlog("signal", "debrief_done", sid=sid[:12], found_session=session is not None)
        if session:
            closed = _close_terminal_tab(session)
            mlog("close", "tab_close_attempt", sid=sid[:12], title=session.title, success=closed)
            if closed:
                cleaned.append(sid)
        else:
            mlog("signal", "debrief_orphan", sid=sid[:12])

        # Clean up the signal file
        try:
            signal_file.unlink()
        except OSError:
            pass

    return cleaned


def copy_to_clipboard(text: str) -> None:
    try:
        subprocess.run(["pbcopy"], input=text.encode(), timeout=4)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _derive_cwd_from_transcript(transcript_path: str) -> str:
    """Derive the launch cwd from the project directory name.

    Claude CLI encodes: /Users/max/proj → -Users-max-proj
    """
    name = Path(transcript_path).parent.name
    if not name.startswith("-"):
        return ""
    decoded = "/" + name[1:].replace("-", "/")
    # Hyphen→slash is lossy (claude-monitor → claude/monitor). Only trust the
    # decoded path if it actually exists; otherwise fall through to next source.
    if not Path(decoded).is_dir():
        return ""
    return decoded


def _snapshot_window_sids() -> set[str]:
    """Return the set of ·sid8 markers currently visible in terminal windows."""
    jxa = """(() => {
        const sidRe = /\u00b7([0-9a-f]{8})/;
        const sids = [];
        for (const appName of ["Ghostty", "iTerm2", "Terminal"]) {
            try {
                const titles = Application(appName).windows.name();
                for (const t of titles) {
                    const m = t && t.match(sidRe);
                    if (m) sids.push(m[1]);
                }
            } catch(e) {}
        }
        return JSON.stringify(sids);
    })()"""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", jxa],
            capture_output=True, text=True, timeout=5,
        )
        return set(json.loads(result.stdout.strip() or "[]"))
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return set()


def _auto_rename_after_resume(old_name: str, expected_sid8: str) -> None:
    """Wait for the resumed session's window to appear, then send /rename.

    The resume KNOWS which sid it resumed (claude --resume preserves the sid),
    so we wait for that exact ·sid8 marker and type only into it. The previous
    snapshot-diff heuristic ("whatever marker is new must be the resumed one")
    could lock onto an unrelated running session whose marker flickered out of
    the before-snapshot during a title rewrite, then type a /rename into it as
    a unique match (observed twice on 2026-06-05: tools-monitor renamed to
    prep-CFITtalk and then to config-claude.md). Never infer what is known.
    """
    deadline = time.time() + 15
    new_sid8 = None
    while time.time() < deadline:
        time.sleep(0.8)
        if expected_sid8 in _snapshot_window_sids():
            new_sid8 = expected_sid8
            break
    if not new_sid8:
        mlog("DIVERGE", "auto_rename_no_new_window",
             name=old_name, expected=expected_sid8)
        return

    name_json = json.dumps(old_name)
    sid_marker = f"\u00b7{new_sid8}"
    jxa = f"""(() => {{
        const se = Application("System Events");
        const marker = "{sid_marker}";
        const renameText = "/rename " + {name_json};
        for (const appName of ["Ghostty", "iTerm2", "Terminal"]) {{
            let titles;
            try {{ titles = Application(appName).windows.name(); }}
            catch(e) {{ continue; }}
            // NEVER type into an ambiguous target. A stale tab can keep an old
            // ·sid8 marker while a different live session runs inside it (a
            // resume in a dead session's tab), so two windows wearing the same
            // marker means we cannot know which one actually hosts the session.
            // A mistyped /rename permanently pollutes the other conversation's
            // stored name (observed: EBC-NAB renamed to tools-monitor).
            const matches = titles.filter(t => t && t.includes(marker));
            if (matches.length > 1) {{
                return "abort_multi_match:" + marker + ":" + matches.length;
            }}
            const target = matches[0];
            if (!target) continue;
            Application(appName).activate();
            delay(0.1);
            const proc = se.processes.byName(appName);
            try {{
                const w = proc.windows.byName(target);
                w.actions["AXRaise"].perform();
                try {{ w.attributes["AXMain"].value = true; }} catch(e) {{}}
            }} catch(e) {{
                try {{
                    const menu = proc.menuBars[0].menuBarItems.byName("Window").menus[0];
                    const item = menu.menuItems.name().find(n => n && n.includes(marker));
                    if (item) menu.menuItems.byName(item).click();
                }} catch(e2) {{}}
            }}
            delay(0.25);
            // Safety: only type if the frontmost window's title actually
            // carries our marker — otherwise we'd rename the wrong session.
            let frontTitle = "";
            try {{ frontTitle = proc.windows[0].name(); }} catch(e) {{}}
            if (!frontTitle || !frontTitle.includes(marker)) {{
                return "abort_wrong_front:" + marker + ":" + frontTitle;
            }}
            se.keystroke(renameText);
            delay(0.1);
            se.keyCode(36);
            return "sent:" + marker;
        }}
        return "not_found:" + marker;
    }})()"""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", jxa],
            capture_output=True, text=True, timeout=8,
        )
        out = result.stdout.strip()
        mlog("resume", "auto_rename", name=old_name, new_sid8=new_sid8,
             result=out)
        if out.startswith("abort_wrong_front:"):
            mlog("DIVERGE", "auto_rename_wrong_front", name=old_name,
                 new_sid8=new_sid8, result=out)
        if out.startswith("abort_multi_match:"):
            mlog("DIVERGE", "auto_rename_multi_match", name=old_name,
                 new_sid8=new_sid8, result=out)
    except (subprocess.TimeoutExpired, OSError):
        mlog("resume", "auto_rename_error", name=old_name, new_sid8=new_sid8)


def _resume_command_for(session: Session) -> tuple[str, str]:
    """(command, cwd) to relaunch this session. Claude CLI resolves sessions
    by hashing the cwd, so the original launch directory matters:
    sessions-index projectPath > transcript path > last cwd. Shared by
    resume_session() and the layout restore so the two cannot drift."""
    # base_sid: a sibling row's id is 'uuid@pid' (or 'uuid#startedms');
    # `claude --resume` wants the bare conversation id. The layout restore
    # test caught this building `--resume uuid@12345` (2026-08-23); the
    # same row reaches resume_session via the menu, so the fix lives here.
    cmd = f"claude --resume {base_sid(session.session_id)}"
    candidates = (
        session.project_path,
        _derive_cwd_from_transcript(session.transcript_path),
        session.cwd,
    )
    # First candidate that still exists on disk: a deleted worktree made
    # `cd /gone && claude ...` fail inside the new surface, which under
    # Ghostty's exec-direct command can close the tab on the spot
    # (review FYI, 2026-08-23). Home is always a valid fallback and
    # `claude --resume <sid>` still finds the conversation by id.
    cwd = next((c for c in candidates if c and Path(c).is_dir()), str(Path.home()))
    return cmd, cwd


def _ghostty_surface_command(cwd: str, cmd: str, title_stamp: str = "") -> str:
    """The one string Ghostty's `command` field execs directly (no shell):
    wrap in `zsh -ic` so PATH from .zshrc applies (see resume_session's
    notes: -i is required, -l alone does not source .zshrc).

    `title_stamp`: emitted as an OSC 0 title before the real command so
    the window is findable by a UNIQUE name in the AX tree from the first
    instant, before `claude` (or a prompt) stamps its own title. Without
    it every fresh window wears the same placeholder and two created back
    to back cannot be told apart (live probe, 2026-08-23). An empty cmd
    (a plain-shell placeholder tab) still gets the stamp, then a shell."""
    parts = []
    if title_stamp:
        parts.append(f"printf '\\033]0;{title_stamp}\\007'")
    parts.append(f"cd {shlex.quote(cwd)}")
    parts.append(cmd if cmd else "exec zsh")
    inner = " && ".join(parts)
    return f"/bin/zsh -ic {shlex.quote(inner)}"


def resume_session(session: Session) -> bool:
    """Resume a Claude session in a new Ghostty window (falls back to Terminal.app).

    Opens the window/runs the command entirely through each app's own native
    scripting, never System Events keystroke/keyCode. Any Accessibility-
    injected synthetic keyboard event, regardless of which key, was found to
    trigger Claude Nest's push-to-talk (observed live 2026-08-15: a bare
    synthetic Cmd+T *and* Cmd+N each fired it from Max's own terminal running
    the identical osascript, while his real physical Cmd+N did not. The
    Nest side reacts to Accessibility-injected input generically, not to a
    specific key). newWindow's own command field sidesteps the whole class
    of collision. Ghostty has no native new-tab equivalent, so this costs
    the tab-in-current-window behavior; iTerm2 support (previously
    keystroke-only) is dropped rather than left as a latent repeat of the
    same collision for any iTerm2 user: it falls through to Terminal.app.
    """
    cmd, cwd = _resume_command_for(session)

    # Verify the JSONL transcript exists before trying to resume
    jsonl_exists = bool(session.transcript_path) and Path(session.transcript_path).exists()
    mlog("resume", "attempt", sid=session.session_id[:12], title=session.title,
         cwd=cwd, jsonl_exists=jsonl_exists)
    if not jsonl_exists:
        mlog("resume", "no_jsonl", sid=session.session_id[:12],
             path=session.transcript_path)
        return False

    # Ghostty's `command` field takes ONE string it execs directly (no shell,
    # confirmed live: a compound "a; b" string fails to launch) — run it
    # through zsh -c instead. -i (interactive) is required: a bare zsh -c,
    # and even zsh -lc (login only), sources neither .zshrc, so `claude`
    # (PATH'd from the `export PATH=...` at the top of .zshrc, not a
    # login-only file) comes back "command not found" — confirmed live,
    # twice, with real Ghostty windows showing that exact error. Double
    # shlex.quote (once for cwd inside the inner command, once for the
    # whole inner command as zsh's -c argument) round-trips correctly even
    # when cwd itself contains a single quote.
    inner_cmd = f"cd {shlex.quote(cwd)} && {cmd}"
    zsh_cmd = _ghostty_surface_command(cwd, cmd)
    jxa = f"""(() => {{
        const zshCmd = {json.dumps(zsh_cmd)};
        try {{
            const ghostty = Application("Ghostty");
            const w = ghostty.newWindow({{withConfiguration: {{command: zshCmd}}}});
            ghostty.activateWindow(w);
            return "Ghostty";
        }} catch (e) {{}}

        // Fall back to Terminal.app (native doScript — no keystroke here either)
        const term = Application("Terminal");
        term.activate();
        term.doScript({json.dumps(inner_cmd)});
        return "Terminal";
    }})()"""

    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", jxa],
            capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        mlog("resume", "launched", sid=session.session_id[:12],
             via=out, rc=result.returncode)
        if result.returncode == 0 and out in ("Ghostty", "Terminal"):
            _recently_resumed[session.session_id] = time.time()
            # Force a fresh PID-map read on the next liveness check. Every
            # caller used to have to remember this itself; one (the jump
            # action's resume-fallback) didn't, so a fast second click could
            # still read a cached pre-resume map and see "not alive",
            # firing a second resume of the same sid — the duplicate then
            # gets kicked by Claude Code's own single-instance guard
            # (observed live 2026-08-15, config-MCPs).
            global _pid_map_ts
            _pid_map_ts = 0
            # Auto-rename the new session to match the old name. Skip junk
            # fallback titles (home-dir basename, sid8, generic placeholders)
            # — applying those would overwrite a better auto-generated name.
            junk = {"", "Claude", "Claude Code", "~",
                    Path.home().name, session.session_id[:8]}
            if session.title and session.title not in junk and len(session.title) >= 3:
                threading.Thread(
                    target=_auto_rename_after_resume,
                    args=(session.title, base_sid(session.session_id)[:8]),
                    daemon=True,
                ).start()
            return True
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        mlog("resume", "launch_error", sid=session.session_id[:12], error=str(e))
        return False


# ── Screens ───────────────────────────────────────────────────────────────────

_SPIN_BASE = "·*✢✳✶✻"
SPINNER_FRAMES = _SPIN_BASE + _SPIN_BASE[-2:0:-1]  # ping-pong: up then back down


def _get_api_key() -> str:
    """Find an Anthropic API key from standard locations."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    for path in [
        Path.home() / "claude-monitor" / ".api_key",
        Path.home() / ".config" / "anthropic" / "api_key",
    ]:
        if path.exists():
            val = path.read_text().strip()
            if val:
                return val
    return ""


def _generate_session_summary(session: Session, time_range: str = "week") -> str:
    """Generate a Haiku summary for a session. Reads transcript, checks for
    existing summaries, and builds incrementally."""
    api_key = _get_api_key()
    if not api_key:
        return "[yellow]No API key found[/] — add key to ~/claude-monitor/.api_key"

    summary_file = HOOK_STATE_DIR / f"{session.session_id}.summary"
    now = time.time()
    lookback = 7 * 86400 if time_range == "week" else 30 * 86400
    period_start = now - lookback

    # Check existing summary
    existing = None
    if summary_file.exists():
        try:
            existing = json.loads(summary_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if existing and existing.get("summaries"):
        latest = existing["summaries"][-1]
        latest_end = latest.get("period_end", "")
        if latest_end:
            try:
                from datetime import datetime
                end_ts = datetime.fromisoformat(latest_end).timestamp()
                if (now - end_ts) < lookback * 0.5:
                    return existing.get("combined", latest.get("text", ""))
            except (ValueError, TypeError):
                pass

    # Read transcript excerpt
    transcript = session.transcript_path
    user_messages = []
    assistant_snippets = []
    if transcript and Path(transcript).exists():
        try:
            lines = Path(transcript).read_text().splitlines()
            for line in lines[-200:]:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "") if isinstance(msg, dict) else ""
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                content = block.get("text", "")
                                break
                    if isinstance(content, str) and content.strip():
                        user_messages.append(content.strip()[:200])
                elif entry.get("type") == "assistant":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                content = block.get("text", "")
                                break
                    if isinstance(content, str) and content.strip():
                        assistant_snippets.append(content.strip()[:150])
        except OSError:
            pass

    if not user_messages:
        return "[dim]No transcript data to summarize[/]"

    # Build context — interleave user and assistant messages
    context_parts = []
    for i, um in enumerate(user_messages[-8:]):
        context_parts.append(f"User: {um}")
        if i < len(assistant_snippets):
            context_parts.append(f"Assistant: {assistant_snippets[-(len(user_messages[-8:]) - i)][:100]}")
    transcript_text = "\n".join(context_parts)

    # Haiku API call
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        prompt = f"Summarize this Claude Code session's activity in 2-3 sentences. Focus on what was accomplished, what's in progress, and any blockers. Be concise.\n\nSession: {session.title}\n\nRecent activity:\n{transcript_text}"

        if existing and existing.get("combined"):
            prompt = f"Previous summary: {existing['combined']}\n\nNew activity since then:\n{transcript_text}\n\nUpdate the summary in 2-3 sentences covering the full history. Focus on what was accomplished and current state."

        resp = client.messages.create(
            model="claude-haiku-4-5-20250414",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        summary_text = resp.content[0].text.strip()
    except Exception as e:
        return f"[red]Summary failed:[/] {_escape_markup(str(e)[:80])}"

    # Store incrementally
    from datetime import datetime
    new_entry = {
        "period_end": datetime.now().isoformat(),
        "text": summary_text,
    }
    if existing and existing.get("summaries"):
        existing["summaries"].append(new_entry)
        existing["combined"] = summary_text
    else:
        existing = {
            "summaries": [new_entry],
            "combined": summary_text,
        }

    try:
        summary_file.write_text(json.dumps(existing, indent=2) + "\n")
    except OSError:
        pass

    return summary_text


LABEL_WIDTH = 16


def time_to_col(ts: float, t_min: float, range_secs: float, chart_width: int) -> int:
    """Map a unix timestamp to a column index [0, chart_width-1]."""
    if range_secs <= 0 or chart_width <= 0:
        return 0
    frac = (ts - t_min) / range_secs
    return max(0, min(chart_width - 1, int(frac * chart_width)))


def generate_ticks(t_min: float, t_max: float, chart_width: int) -> list[tuple[int, str]]:
    """Generate (column_index, label) tick marks for a time axis."""
    from datetime import datetime as dt
    range_secs = t_max - t_min
    if range_secs <= 0 or chart_width <= 0:
        return []

    if range_secs < 7200:       # < 2h → 15-min ticks
        interval = 900
        fmt = "%-I:%M"
    elif range_secs < 86400:    # < 24h → hourly
        interval = 3600
        fmt = "%-I%p"
    elif range_secs < 604800:   # < 7d → daily
        interval = 86400
        fmt = "%a"
    else:                       # >= 7d → date
        interval = 86400
        fmt = "%b %-d"

    # Round t_min up to the next interval boundary
    first = ((int(t_min) // interval) + 1) * interval
    ticks = []
    last_col = -999
    t = first
    while t < t_max:
        col = time_to_col(t, t_min, range_secs, chart_width)
        label = dt.fromtimestamp(t).strftime(fmt).lower().replace("am", "a").replace("pm", "p")
        if col - last_col >= len(label) + 2:
            ticks.append((col, label))
            last_col = col
        t += interval
    return ticks


class ApiKeyPrompt(ModalScreen[str | None]):
    """Prompt for an Anthropic API key."""
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Save"),
    ]
    DEFAULT_CSS = """
    ApiKeyPrompt { align: center middle; }
    #apikey-box { width: 70; height: auto; padding: 1 2; background: $panel; border: solid $primary; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="apikey-box"):
            yield Label("[bold]Anthropic API Key[/]")
            yield Label("[dim]Used for session summaries and title generation. "
                        "Stored at ~/claude-monitor/.api_key[/]")
            yield Input(
                placeholder="sk-ant-...",
                password=True,
                id="apikey-input",
            )
        yield Footer()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save(event.value.strip())

    def action_submit(self) -> None:
        self._save(self.query_one("#apikey-input", Input).value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self, key: str) -> None:
        if not key:
            self.dismiss(None)
            return
        key_dir = Path.home() / "claude-monitor"
        key_dir.mkdir(exist_ok=True)
        key_file = key_dir / ".api_key"
        key_file.write_text(key + "\n")
        key_file.chmod(0o600)
        self.dismiss(key)


class RenamePrompt(ModalScreen[str | None]):
    """Inline text prompt for editing a session name."""
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Submit"),
    ]
    CSS = """
    RenamePrompt { align: center middle; }
    #rename-box {
        width: 60; height: auto; padding: 1 2;
        background: $panel; border: thick $accent;
    }
    #rename-input { width: 100%; }
    """

    def __init__(self, current: str) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-box"):
            yield Label("Rename session [dim](sends /rename <name>)[/]")
            yield Input(value=self._current, id="rename-input")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#rename-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        self.dismiss(name or None)

    def action_submit(self) -> None:
        name = self.query_one("#rename-input", Input).value.strip()
        self.dismiss(name or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PromptSend(ModalScreen[str | None]):
    """Type a prompt and send it to one session's terminal.

    Max, 2026-08-28: "make the spacebar allow us to send a prompt to a
    claude, similar to how it works in claude code pressing left arrow."
    Same shape as RenamePrompt, but the text goes to the session verbatim
    instead of being wrapped in a command, and the box names its target so
    a prompt is never typed at the wrong Claude by accident."""
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Submit"),
    ]
    CSS = """
    PromptSend { align: center middle; }
    #prompt-box {
        width: 78; height: auto; padding: 1 2;
        background: $panel; border: thick $accent;
    }
    #prompt-input { width: 100%; }
    """

    def __init__(self, target_title: str) -> None:
        super().__init__()
        self._target = target_title

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(f"Send to [bold]{_escape_markup(self._target)}[/]"
                        f"  [dim]Enter sends · Esc cancels[/]")
            yield Input(placeholder="your prompt…", id="prompt-input")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#prompt-input", Input).value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionMenu(ModalScreen[str]):
    BINDINGS = [
        Binding("escape", "dismiss_menu", "Close"),
        Binding("q", "dismiss_menu", "Close", show=False),
        Binding("enter", "select", "Select"),
    ]
    CSS = """
    SessionMenu { background: rgba(0, 0, 0, 0.5); align: center middle; }
    #menu-container {
        width: 44; max-height: 18;
        background: $surface; border: solid $primary; padding: 1 2;
    }
    #menu-title { text-align: center; text-style: bold; padding-bottom: 1; }
    #menu-options { height: auto; }
    """

    def __init__(self, session: Session, context: str = "") -> None:
        super().__init__()
        self.session = session
        self.menu_context = context

    def compose(self) -> ComposeResult:
        s = self.session
        options = []
        if s.status in INACTIVE_STATUSES:
            options.append(Option("▶   Resume session", id="resume"))
        else:
            options.append(Option("🖥   Jump to terminal", id="jump"))
            options.append(Option("▶   Resume in new window", id="resume"))
            options.append(Option("🏷   Rename…", id="edit_name"))
        if self.menu_context == "timeline":
            options.append(Option("📊  Summarize period", id="summarize"))
        options.append(Option(f"📋  Copy session ID ({s.session_id[:8]}…)", id="copy_id"))
        if s.remote_url:
            options.append(Option("🔗  Open remote control", id="remote"))
        options.append(Option("📂  Open transcript", id="transcript"))
        if s.status not in INACTIVE_STATUSES:
            options.append(Option("❌  Kill process", id="kill"))
        options.append(Option("─" * 26, id="sep", disabled=True))
        options.append(Option("◀   Back", id="close"))

        with Vertical(id="menu-container"):
            yield Label(f"[bold]{s.title}[/]", id="menu-title")
            yield OptionList(*options, id="menu-options")
        yield Footer()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_select(self) -> None:
        ol = self.query_one("#menu-options", OptionList)
        if ol.highlighted is not None:
            opt = ol.get_option_at_index(ol.highlighted)
            self.dismiss(opt.id)

    def action_dismiss_menu(self) -> None:
        self.dismiss("close")


class ColumnPicker(ModalScreen[list[str]]):
    BINDINGS = [
        Binding("escape", "done", "Done"),
        Binding("enter", "toggle_col", "Toggle"),
        Binding("space", "toggle_col", "Toggle", show=False),
        Binding("shift+up", "move_up", "↑ Move"),
        Binding("shift+down", "move_down", "↓ Move"),
    ]
    CSS = """
    ColumnPicker { background: rgba(0, 0, 0, 0.5); align: center middle; }
    #picker-container {
        width: 38; height: auto; max-height: 22;
        background: $surface; border: solid $primary; padding: 1 2;
    }
    #picker-title { text-align: center; text-style: bold; padding-bottom: 1; }
    #picker-hint { text-align: center; color: $text-muted; padding-top: 1; }
    #picker-list { height: auto; max-height: 14; }
    """

    def __init__(self, visible: list[str], col_order: list[str]) -> None:
        super().__init__()
        self.selected_cols = set(visible)
        self._col_keys = list(col_order)

    def compose(self) -> ComposeResult:
        options = []
        for key in self._col_keys:
            info = ALL_COLUMNS[key]
            check = "✓" if key in self.selected_cols else " "
            options.append(Option(f"[green]{check}[/]  {info['label']}", id=key))
        with Vertical(id="picker-container"):
            yield Label("[bold]Column Picker[/]", id="picker-title")
            yield OptionList(*options, id="picker-list")
        yield Footer()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter key on OptionList fires this — use it to toggle."""
        event.stop()
        self.action_toggle_col()

    def action_toggle_col(self) -> None:
        ol = self.query_one("#picker-list", OptionList)
        if ol.highlighted is None:
            return
        key = self._col_keys[ol.highlighted]
        if key in self.selected_cols:
            self.selected_cols.discard(key)
        else:
            self.selected_cols.add(key)
        info = ALL_COLUMNS[key]
        check = "✓" if key in self.selected_cols else " "
        ol.replace_option_prompt(key, f"[green]{check}[/]  {info['label']}")

    def _swap_options(self, idx_a: int, idx_b: int) -> None:
        """Swap two items in the list and update the OptionList display."""
        self._col_keys[idx_a], self._col_keys[idx_b] = self._col_keys[idx_b], self._col_keys[idx_a]
        # Rebuild the whole list (OptionList doesn't have a swap API)
        ol = self.query_one("#picker-list", OptionList)
        ol.clear_options()
        for key in self._col_keys:
            info = ALL_COLUMNS[key]
            check = "✓" if key in self.selected_cols else " "
            ol.add_option(Option(f"[green]{check}[/]  {info['label']}", id=key))
        ol.highlighted = idx_b

    def action_move_up(self) -> None:
        ol = self.query_one("#picker-list", OptionList)
        idx = ol.highlighted
        if idx is None or idx == 0:
            return
        self._swap_options(idx, idx - 1)

    def action_move_down(self) -> None:
        ol = self.query_one("#picker-list", OptionList)
        idx = ol.highlighted
        if idx is None or idx >= len(self._col_keys) - 1:
            return
        self._swap_options(idx, idx + 1)

    def action_done(self) -> None:
        # Return columns in their current order, filtered to selected
        cols = [k for k in self._col_keys if k in self.selected_cols]
        self.dismiss(cols)


# ── Statusline parts inventory ───────────────────────────────────────────────

STATUSLINE_PARTS = {
    "compact_str": {"label": "Compaction indicators (✻)", "line": 1, "default": True},
    "ctx_warn":    {"label": "Compaction warning (⚠)",    "line": 1, "default": True},
    "fast_mode":   {"label": "/fast indicator",           "line": 1, "default": True},
    "quota_bar":   {"label": "Usage quota ammo bar",      "line": 2, "default": True},
    "quota_reset": {"label": "Quota reset timer",         "line": 2, "default": False},
    "tokens":      {"label": "Token count",               "line": 2, "default": False},
    "cost":        {"label": "Session cost ($)",           "line": 2, "default": False},
    "model":       {"label": "Model name",                "line": 2, "default": False},
}

STATUSLINE_DEFAULTS = {k: v["default"] for k, v in STATUSLINE_PARTS.items()}


def load_statusline_prefs() -> dict[str, bool]:
    """Load statusline prefs, falling back to defaults for missing keys."""
    prefs = load_prefs()
    saved = prefs.get("statusline", {})
    merged = dict(STATUSLINE_DEFAULTS)
    merged.update({k: v for k, v in saved.items() if k in STATUSLINE_DEFAULTS})
    return merged


def _render_mock_preview(sl_prefs: dict[str, bool]) -> str:
    """Return a Rich-markup mock preview of the statusline."""
    # Line 1: ctx bar (always on) + optional parts
    ctx_bar = "ctx [green]████[/][yellow]██[/][dim]░░[/][red]▒▒[/] 47%"
    line1_extras = []
    if sl_prefs.get("compact_str"):
        line1_extras.append("[yellow]✻✻[/]")
    if sl_prefs.get("ctx_warn"):
        line1_extras.append("[bold red]⚠ compact soon[/]")
    if sl_prefs.get("fast_mode"):
        line1_extras.append("[cyan]/fast[/]")

    line1 = ctx_bar
    if line1_extras:
        # compact_str appends directly (no separator), others get separator
        for i, extra in enumerate(line1_extras):
            if i == 0 and sl_prefs.get("compact_str") and extra.startswith("[yellow]✻"):
                line1 += f" {extra}"
            else:
                line1 += f" [dim]│[/] {extra}"

    # Line 2: quota bar (if enabled) + optional parts
    line2_parts = []
    if sl_prefs.get("quota_bar"):
        line2_parts.append("[blue]▮▮▮▮▮▮▮▮[/][dim]▯▯[/]  8%")
    if sl_prefs.get("quota_reset"):
        line2_parts.append("resets 4h32m")
    if sl_prefs.get("tokens"):
        line2_parts.append("15k tok")
    if sl_prefs.get("cost"):
        line2_parts.append("$1.23")
    if sl_prefs.get("model"):
        line2_parts.append("Opus 4.6")

    lines = [line1]
    if line2_parts:
        line2 = "use " + (" [dim]│[/] ".join(line2_parts))
        lines.append(line2)

    return "\n".join(lines)


class StatuslineConfig(ModalScreen[dict[str, bool] | None]):
    BINDINGS = [
        Binding("escape", "done", "Done"),
        Binding("enter", "toggle_part", "Toggle"),
        Binding("space", "toggle_part", "Toggle", show=False),
    ]
    CSS = """
    StatuslineConfig { background: rgba(0, 0, 0, 0.5); align: center middle; }
    #sl-container {
        width: 52; height: auto; max-height: 28;
        background: $surface; border: solid $primary; padding: 1 2;
    }
    #sl-title { text-align: center; text-style: bold; padding-bottom: 1; }
    #sl-preview {
        padding: 0 1; margin-bottom: 1;
        background: $boost; border: solid $accent;
    }
    #sl-hint { text-align: center; color: $text-muted; padding-top: 1; }
    #sl-list { height: auto; max-height: 12; }
    """

    def __init__(self, sl_prefs: dict[str, bool]) -> None:
        super().__init__()
        self.sl_prefs = dict(sl_prefs)
        self._part_keys = list(STATUSLINE_PARTS.keys())

    def compose(self) -> ComposeResult:
        options = []
        for key in self._part_keys:
            info = STATUSLINE_PARTS[key]
            check = "✓" if self.sl_prefs.get(key) else " "
            line_tag = f"L{info['line']}"
            options.append(Option(f"[green]{check}[/]  {info['label']}  [dim]{line_tag}[/]", id=key))
        with Vertical(id="sl-container"):
            yield Label("[bold]Statusline Config[/]", id="sl-title")
            yield Static(_render_mock_preview(self.sl_prefs), id="sl-preview")
            yield OptionList(*options, id="sl-list")
            yield Label("[dim]Enter/Space toggle · Esc done[/]", id="sl-hint")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_toggle_part()

    def action_toggle_part(self) -> None:
        ol = self.query_one("#sl-list", OptionList)
        if ol.highlighted is None:
            return
        key = self._part_keys[ol.highlighted]
        self.sl_prefs[key] = not self.sl_prefs.get(key, False)
        info = STATUSLINE_PARTS[key]
        check = "✓" if self.sl_prefs[key] else " "
        line_tag = f"L{info['line']}"
        ol.replace_option_prompt(key, f"[green]{check}[/]  {info['label']}  [dim]{line_tag}[/]")
        # Update preview
        self.query_one("#sl-preview", Static).update(_render_mock_preview(self.sl_prefs))

    def action_done(self) -> None:
        self.dismiss(self.sl_prefs)


# ── Main App ──────────────────────────────────────────────────────────────────


def footer_items_from_bindings(bindings) -> list[tuple[str, str, str]]:
    """The hotkey line, derived from BINDINGS: every binding with
    show=True, in declaration order, as (letter, label, prefix). "^" for
    Ctrl+letter, "⇧" for a Shift-letter binding ("R"), "" for a plain
    letter ("n"). Derived rather than hand-listed so a new or changed
    hotkey cannot ship without its footer entry (Max, 2026-08-24: "make a
    note that hotkeys need these so they ship when we add or change
    one"); the hand-kept list had drifted twice (History showed ^H while
    bound to Ctrl+Z; Ctrl+L was missing). A binding's `show` flag is the
    one switch: flip it and the footer follows."""
    items = []
    for b in bindings:
        if not getattr(b, "show", True):
            continue
        key, label = b.key, b.description or b.action
        if key.startswith("ctrl+") and len(key) == 6:
            items.append((key[-1].upper(), label, "^"))
        elif len(key) == 1 and key.isalpha():
            items.append((key, label, "⇧" if key.isupper() else ""))
        else:
            items.append((key, label, ""))
    return items


class DosFooter(Static):
    """DOS-style hotkey bar: the key letter is highlighted within the word."""

    DEFAULT_CSS = """
    DosFooter { dock: bottom; height: 1; background: $panel; padding: 0 1; }
    #update-banner { height: 1; background: $boost; color: $warning; display: none; padding: 0 2; }
    """

    def __init__(self, items: list[tuple[str, str]], **kwargs) -> None:
        # items: [(key, label), ...], optionally [(key, label, prefix)] for
        # a plain-letter exception (Max, 2026-08-16: "n" for jump-next).
        # Default prefix "^" (Ctrl+<key>, plain letters are the type-ahead
        # group jump); pass "" for a key that's deliberately unmodified.
        parts = []
        for item in items:
            key, label = item[0], item[1]
            prefix = item[2] if len(item) > 2 else "^"
            idx = label.lower().find(key.lower())
            if idx >= 0:
                before = label[:idx]
                letter = label[idx]
                after = label[idx + 1:]
                parts.append(f"{prefix}{before}[bold #D97757]{letter}[/]{after}")
            else:
                parts.append(f"{prefix}[bold #D97757]{key}[/] {label}")
        super().__init__("  ".join(parts), **kwargs)


class StatsBar(Horizontal):
    def compose(self) -> ComposeResult:
        yield Label("", id="stats-working")
        yield Label("", id="stats-approve")
        yield Label("", id="stats-done")
        yield Label("", id="stats-closed")
        yield Label("", id="stats-total-cost")
        yield Label("", id="stats-sort")
        yield Label("", id="stats-inbox")
        yield Input(placeholder="🔍 filter...", id="search-bar")
        yield Label("[dim]/ search[/]", id="search-hint")

    def update_stats(self, sessions: list[Session], sort_mode: SortMode,
                     dark: bool = True, inbox_mode: bool = False) -> None:
        working = sum(1 for s in sessions if s.status == "working")
        approve = sum(1 for s in sessions if s.status == "needs_approval")
        done = sum(1 for s in sessions if s.status == "done")
        closed = sum(1 for s in sessions if s.status == "closed")
        total_cost = sum(s.cost for s in sessions)

        # `dark` is the caller's job to supply (from _refresh_apply's own
        # sys_dark, the same value render_row()/render_status_cell() use),
        # not this method's to independently derive from self.app.theme:
        # two separately-computed light/dark checks are only coincidentally
        # in sync today via _sync_system_theme, and would silently diverge
        # the moment either path changes (caught by review, 2026-08-17).
        ready_color = READY_COLOR_DARK if dark else READY_COLOR_LIGHT
        self.query_one("#stats-working", Label).update(f" [green]● {working} working[/]  ")
        self.query_one("#stats-approve", Label).update(f" [yellow]◉ {approve} approve[/]  " if approve else "")
        self.query_one("#stats-done", Label).update(f" [{ready_color}]○ {done} ready[/]  ")
        self.query_one("#stats-closed", Label).update(f" [rgb(100,100,100)]⊘ {closed} closed[/]  " if closed else "")
        self.query_one("#stats-total-cost", Label).update(f" [cyan]Σ ${total_cost:.2f}[/]  ")
        self.query_one("#stats-sort", Label).update(f" [magenta]sort: {sort_mode.label}[/]")
        # A visible chip whenever rows are being hidden on purpose, so a
        # half-empty table never reads as "sessions vanished". The counters
        # above are computed from the UNFILTERED list (inbox is a display
        # filter on `flat`, not on `sessions`), so "12 working" stays true
        # while those 12 rows are hidden; the chip says how many are.
        hidden = sum(1 for s in sessions if s.status not in ACTIONABLE_STATUSES
                     and not s.is_subagent)
        self.query_one("#stats-inbox", Label).update(
            f"  [bold reverse] INBOX [/][dim] {hidden} hidden[/]" if inbox_mode else "")


class SessionTable(DataTable):
    """DataTable that owns mouse clicks: single-click highlights only,
    double-click posts RowDoubleClicked. Keyboard Enter still fires the
    stock RowSelected (→ context menu)."""

    class RowDoubleClicked(Message):
        def __init__(self, row_key: RowKey) -> None:
            self.row_key = row_key
            super().__init__()

    class RowShiftClicked(Message):
        def __init__(self, row_index: int) -> None:
            self.row_index = row_index
            super().__init__()

    def on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        row = meta.get("row")
        if row is None or row < 0:
            return  # header / out-of-bounds — let default handling run
        event.prevent_default()
        event.stop()
        if event.shift:
            self.post_message(self.RowShiftClicked(row))
            return
        self.cursor_coordinate = Coordinate(row, meta.get("column", 0) or 0)
        if event.chain == 2:
            try:
                key = self.ordered_rows[row].key
            except IndexError:
                return
            self.post_message(self.RowDoubleClicked(key))


class ClaudeMonitor(App):
    TITLE = "Claude Monitor"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $surface; }
    StatsBar {
        height: 1; padding: 0 1; background: $boost; dock: top;
    }
    StatsBar Label { width: auto; }
    #search-bar {
        width: 22; height: 1; border: none; padding: 0; margin-left: 2;
        background: transparent; display: none;
    }
    #search-bar:focus { display: block; background: $boost; }
    #search-hint { width: auto; dock: right; }
    #session-table { height: 1fr; }
    /* The cursor row keeps its own text colors. Textual's default focused
       cursor paints a solid $primary band AND forces the row's foreground
       to white, so the READY blue (light mode) or yellow (dark) vanished
       on exactly the row Max had selected, and reappeared the moment the
       window lost focus and the band went translucent. Measured
       2026-08-18: focused fg=(255,255,255), blurred fg=row's own. A
       translucent band with `color: auto` off lets every status color
       survive under the cursor in both focus states. */
    #session-table > .datatable--cursor {
        background: $primary 35%;
        color: $foreground;
        text-style: none;
    }
    #session-table:focus > .datatable--cursor {
        background: $primary 45%;
        color: $foreground;
        text-style: none;
    }
    #detail-panel {
        height: auto; max-height: 35%; min-height: 5; padding: 0 2;
        background: $boost; dock: bottom; border-top: solid $primary;
        overflow-y: auto;
    }
    """

    # Plain letters (no modifier) are reserved for the type-ahead group jump
    # (see on_key): every command that used to sit on a bare letter now
    # requires Ctrl, so typing a group's name never fires a hotkey mid-word.
    # Shift-bound (K/R/P) and non-letter bindings are untouched. Plain "n" is
    # a second deliberate exception (Max, 2026-08-16): jump-to-next-actionable
    # is meant to be a single unmodified keystroke, so no group name starting
    # with "n" can be reached via type-ahead — the same tradeoff K/R/P already
    # accepted for their own letters.
    #
    # Ctrl+H and Ctrl+I are NOT ctrl+letter to Textual: the xterm input
    # protocol Textual speaks (drivers/linux_driver.py has no Kitty/enhanced
    # keyboard negotiation) encodes them identically to Backspace and Tab, so
    # a binding on either is permanently unreachable, not merely a bug to
    # fix. archived/detail moved to z/v to dodge that; verified live that
    # Ctrl+H and Backspace fire the same action and Ctrl+I is silently
    # swallowed like Tab. Ctrl+S is fine here (linux_driver.py explicitly
    # clears IXON/IXOFF), a tmux-nested test transiently ate it.
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+s", "cycle_sort", "Sort"),
        Binding("ctrl+a", "toggle_subagents", "Agents"),
        Binding("ctrl+z", "toggle_archived", "History"),
        Binding("ctrl+c", "pick_columns", "Columns"),
        # Binding("l", "statusline_config", "Statusline"),  # TODO: re-enable after statusline merge
        Binding("ctrl+d", "toggle_debug", "Debug", show=False),
        Binding("K", "setup_api_key", "API Key", show=False),
        Binding("slash", "start_search", "Search", show=False),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("down", "search_to_table", show=False),
        Binding("R", "restart", "Refresh"),
        Binding("n", "jump_next_actionable", "Next"),
        Binding("ctrl+j", "cursor_down", "↓", show=False),
        Binding("ctrl+n", "edit_name", "Name"),
        Binding("space", "send_prompt", "Prompt"),
        Binding("P", "proactive_group", "/proactive→group", show=False),
        Binding("ctrl+g", "toggle_groups", "Group"),
        Binding("ctrl+v", "toggle_detail", "Info", show=False),
        Binding("pageup", "prev_group", "PgUp", show=False, priority=True),
        Binding("pagedown", "next_group", "PgDn", show=False, priority=True),
        Binding("home", "table_home", "Home", show=False, priority=True),
        Binding("end", "table_end", "End", show=False, priority=True),
        Binding("ctrl+p", "toggle_pin", "Pin", show=False),
        Binding("ctrl+o", "toggle_hide_inactive_pins", "Pins", show=False),
        Binding("ctrl+b", "toggle_inbox_mode", "Inbox", show=False),
        Binding("ctrl+l", "save_layout", "Layout"),
        Binding("backspace", "hide_selected", "Hide", show=False),
        Binding("delete", "hide_selected", "Hide", show=False),
        Binding("shift+up", "extend_selection(-1)", "Select↑", show=False, priority=True),
        Binding("shift+down", "extend_selection(1)", "Select↓", show=False, priority=True),
    ]

    sort_mode: reactive[SortMode] = reactive(SortMode.ALPHA)
    show_subagents: reactive[bool] = reactive(False)
    show_archived: reactive[bool] = reactive(False)
    show_scheduled: reactive[bool] = reactive(False)
    show_groups: reactive[bool] = reactive(True)
    show_detail: reactive[bool] = reactive(True)
    hide_inactive_pins: reactive[bool] = reactive(False)
    inbox_mode: reactive[bool] = reactive(False)
    debug_logging: reactive[bool] = reactive(True)  # ON by default
    sessions: list[Session] = []
    _flat_rows: list[Session] = []
    _row_map: list["Session | None"] = []
    _group_counts: dict[str, int] = {}
    _group_header_rows: list[int] = []
    _selected_key: str | None = None
    _visible_cols: list[str] = []
    _col_order: list[str] = []
    _filter: str = ""
    # NOTE: the mutable containers below are (re)created per instance in
    # __init__. Declared here only for the type hints. As class attributes
    # with literal defaults they were ONE dict shared by every
    # ClaudeMonitor() in the process; harmless in production (one
    # instance) but every test's fresh app inherited the previous test's
    # bells and status history, which surfaced as order-dependent failures
    # (found 2026-08-18 while adding inbox mode).
    _dismissing_sessions: dict[str, str]  # sid -> "debriefing" | "closing"
    _dismiss_failed: set[str]  # sids where dismiss failed (can't reach terminal)
    _prev_statuses: dict[str, str]  # sid -> previous status (for transition logging)
    _bell: dict[str, dict]  # sid -> {"rang_at": float, "acked": bool}
    _pulse_phase: bool = False
    _first_cell_base: dict[str, str]
    _spin_idx: int = 0
    _last_cursor_row: int = 0
    _hidden: set[str] = set()
    _pinned: set[str] = set()
    _selection: set[str] = set()
    _selection_anchor: str | None = None
    _delete_armed_for: frozenset[str] | None = None
    _typeahead_buffer: str = ""
    _typeahead_last_key: float = 0.0
    _last_rendered: dict[str, list[str]] = {}
    _extending_cursor: bool = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Per-instance containers; see the note above the declarations.
        self._dismissing_sessions = {}
        self._dismiss_failed = set()
        self._prev_statuses = {}
        self._bell = {}
        self._first_cell_base = {}
        self._last_rendered = {}
        self._hidden = set()
        self._pinned = set()
        self._selection = set()

    def notify(self, message, *, timeout: float | None = 5, **kwargs):
        """Override to log every toast notification."""
        mlog("toast", "notify", message=str(message))
        super().notify(message, timeout=timeout, **kwargs)

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatsBar()
        yield Static("", id="update-banner")
        yield SessionTable(id="session-table", cursor_type="row")
        yield Static(
            "",
            id="detail-panel"
        )
        yield DosFooter(footer_items_from_bindings(self.BINDINGS))

    def on_mount(self) -> None:
        t0 = time.perf_counter()
        try:
            from monitor_log import log as _startup_log
            _startup_log("startup", "launch", state_home=str(_STATE_HOME),
                         staging=(os.environ.get("MONITOR_STATE_HOME") is not None))
        except Exception:
            pass
        self.register_theme(GRUVBOX_DARK)
        self.register_theme(GRUVBOX_LIGHT)
        self._visible_cols = get_visible_columns()
        self._col_order = get_column_order()
        self._hidden = load_hidden_sessions()
        self._pinned = load_pinned_sessions()
        self._selection = set()
        self._load_view_state()
        self.theme = "gruvbox-dark" if _system_is_dark() else "gruvbox-light"
        t0 = _perf("on_mount: load_prefs (cols+theme)", t0)
        self._rebuild_table_columns()
        t0 = _perf("on_mount: _rebuild_table_columns", t0)
        # Set terminal window title with ·MONITOR marker for jumpback.
        # Textual captures stdout, so write to /dev/tty directly.
        if "PYTEST_CURRENT_TEST" not in os.environ:
            try:
                with open("/dev/tty", "w") as tty:
                    tty.write("\033]2;◇ Claude Monitor ·MONITOR\007")
                    tty.flush()
            except OSError:
                pass
        t0 = _perf("on_mount: /dev/tty title write", t0)
        # One-time sweep: heal stale hook states and re-stamp terminal titles
        if "PYTEST_CURRENT_TEST" not in os.environ:
            threading.Thread(target=_reconcile_sessions, daemon=True).start()
        self.refresh_sessions()
        t0 = _perf("on_mount: first refresh_sessions (schedule)", t0)
        self.set_interval(3, self.refresh_sessions)
        self.set_interval(0.132, self._tick_spinner)
        self.set_interval(0.6, self._tick_bell)
        self.set_interval(0.2, self._check_jump_request)
        if "PYTEST_CURRENT_TEST" not in os.environ:
            self._start_jump_server()
        self.set_interval(30, self._periodic_reconcile)
        self.set_interval(600, self._check_updates)
        self.set_interval(600, self._audit_stats)  # Every 10 minutes
        # Initial update check after a brief delay
        self.set_timer(5, self._check_updates)
        self.query_one("#session-table", DataTable).focus()
        mlog("app", "started")
        t0 = _perf("on_mount: set_interval + focus + mlog", t0)

        # Teach the jumpback hotkey for the first 20 launches
        launches = load_prefs().get("launch_count", 0) + 1

        def _bump(prefs: dict) -> bool:
            prefs["launch_count"] = prefs.get("launch_count", 0) + 1
            return True

        _update_prefs(_bump)
        if launches <= 20:
            self.notify(
                "Press [b]Ctrl+Shift+Space[/] from any app to return here "
                f"[dim]({21 - launches} more reminders)[/]",
                title="jumpback", timeout=6,
            )
        _perf("on_mount: launch_count save_prefs + notify", t0)

    def _start_jump_server(self) -> None:
        """Serve http://localhost:48624/jump/<sid8-or-name> so cross-session
        mentions in any Claude's output can be cmd+clicked. Ghostty's URL
        detector matches http://, the browser opens the URL, the handler drops
        the target into the request file (picked up by the 200ms poller), and
        the raised terminal window covers the browser a beat later. Bound to
        127.0.0.1 only. Silently skipped if the port is already taken (another
        monitor instance owns it)."""
        request_path = JUMP_REQUEST_PATH

        class _JumpHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: N802 - silence stderr
                pass

            def do_GET(self):  # noqa: N802
                from urllib.parse import unquote
                if not self.path.startswith("/jump/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                target = unquote(self.path[len("/jump/"):]).strip("/").strip()
                # sid8, full uuid, or a session title — same charset the
                # request-file path accepts. Reject anything suspicious.
                if not target or len(target) > 80 or any(c in target for c in "\n\r\0/"):
                    self.send_response(400)
                    self.end_headers()
                    return
                try:
                    request_path.write_text(target)
                except OSError:
                    pass
                mlog("jump", "http_request", target=target)
                body = (b"<!doctype html><title>jumping</title>"
                        b"<body style='font-family:monospace;background:#282828;"
                        b"color:#b2ebbb;padding:2em'>jumping\xe2\x80\xa6"
                        b"<script>setTimeout(()=>window.close(),400)</script>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", JUMP_HTTP_PORT), _JumpHandler)
        except OSError:
            # Another monitor instance owns the port, so it also owns the
            # request file: stand down from polling it. Otherwise both
            # instances race the 200ms poll and whichever ticks first
            # serves Ctrl+Shift+N against ITS toggle-filtered session list
            # and advances the shared next_cursor (advisor review,
            # 2026-08-18; a duplicate monitor was accidentally spawned
            # once that day). Only the instance whose HTTP listener is
            # live is the one _a_monitor_is_running() actually detected.
            self._owns_jump_requests = False
            mlog("jump", "http_port_busy", port=JUMP_HTTP_PORT)
            return
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mlog("jump", "http_listening", port=JUMP_HTTP_PORT)

    _owns_jump_requests: bool = True

    def _check_jump_request(self) -> None:
        """Poll for a jump request dropped by claude-jump (e.g. from the
        ClaudeJump Shortcut, whose sandbox can't send Apple Events itself)
        or by `claude-monitor --jump-next` (Ctrl+Shift+N). Runs every 200ms;
        the file-exists check is the only cost. --jump-next drops this
        instead of scanning sessions itself because a cold parse_sessions()
        in a brand-new process took ~6.5s against this machine's real
        session count (measured 2026-08-16) versus near-zero here, where
        self.sessions is already warm from the running monitor's own
        3-second refresh loop."""
        if not self._owns_jump_requests:
            return  # a sibling instance owns the port and the request file
        try:
            if not JUMP_REQUEST_PATH.exists():
                return
            target = JUMP_REQUEST_PATH.read_text().strip()
            JUMP_REQUEST_PATH.unlink()
        except OSError:
            return
        if not target:
            return
        # startswith, not ==: --jump-next/--restart append a unique
        # ":<pid>:<monotonic-ns>" token so a caller can tell its own
        # request from one dropped by a second overlapping invocation
        # (see _drop_request_and_await_consumption).
        if target.startswith(JUMP_NEXT_SENTINEL):
            self._handle_jump_next_request()
            return
        if target.startswith(RESTART_SENTINEL):
            mlog("jump", "restart_request")
            self.action_restart()
            return
        mlog("jump", "request_file", target=target)
        # Match by sid8 prefix first, then by exact title.
        s = next((x for x in self.sessions
                  if x.session_id.startswith(target)), None)
        if s is None:
            s = next((x for x in self.sessions if x.title == target), None)
        if s is None:
            mlog("jump", "request_no_match", target=target)
            self.notify(f"Jump: no session matching {target[:20]}", timeout=3)
            return
        self._ack_bell(s.session_id)

        def _jump_and_mark(s=s):
            if focus_terminal_session(s):
                _mark_ready_seen(s.session_id, s.status, s.last_activity)

        self.run_worker(_jump_and_mark, thread=True)

    def _handle_jump_next_request(self) -> None:
        """The fast path for Ctrl+Shift+N: same selection and outcome as
        the headless jump_to_next_actionable(), but against self.sessions
        (already warm, already deduped by dedupe_scheduled_sessions() in
        this instance's own refresh) instead of a fresh parse_sessions()."""
        prefs = load_prefs()
        acked_ready = _normalize_acked_ready(prefs.get("acked_ready", {}))
        target = find_next_actionable(self.sessions, prefs.get("next_cursor"), acked_ready)
        if target is None:
            mlog("jump", "next_request_none")
            self.notify("Nothing needs you right now", timeout=3)
            return
        _update_prefs(lambda p: p.__setitem__("next_cursor", target.session_id) or True)
        self._ack_bell(target.session_id)
        mlog("jump", "next_request", sid=target.session_id[:12], title=target.title)

        def _jump_and_notify(s=target):
            ok, message = _focus_or_resume_target(s)
            if not ok:
                self.notify(message, timeout=6, severity="warning")

        self.run_worker(_jump_and_notify, thread=True)

    def _periodic_reconcile(self) -> None:
        """Run the reconciliation sweep in a background thread every 30s."""
        threading.Thread(target=_reconcile_sessions, daemon=True).start()

    def _check_updates(self) -> None:
        """Check for upstream updates in a background thread."""
        def _worker():
            _check_for_updates()
            mlog("update", "check_done", available=_update_available or "(none)")
            if _update_available:
                self.call_from_thread(self._show_update_banner)
        threading.Thread(target=_worker, daemon=True).start()

    def _show_api_key_hint(self) -> None:
        """Show a one-time hint about setting up an API key."""
        self.notify(
            "No API key configured — session summaries disabled.\n"
            "Add key: echo 'sk-ant-...' > ~/claude-monitor/.api_key",
            timeout=10,
        )

    def action_setup_api_key(self) -> None:
        """Open the API key setup prompt."""
        def _on_result(key: str | None) -> None:
            if key:
                self.notify(f"API key saved ({key[:12]}...)", timeout=4)
            else:
                self.notify("API key setup cancelled", timeout=3)
        self.push_screen(ApiKeyPrompt(), callback=_on_result)

    def _show_update_banner(self) -> None:
        """Show a persistent update banner below the stats bar."""
        try:
            banner = self.query_one("#update-banner", Static)
            banner.update(
                f"[bold #D97757]⬆ Update:[/] {_escape_markup(_update_available)}"
                "  [dim]Press Shift+R to update[/]"
            )
            banner.display = True
        except Exception:
            pass

    def _tick_spinner(self) -> None:
        """Advance spinner frame and update only working-status cells."""
        if "status" not in self._visible_cols:
            return
        self._spin_idx += 1
        try:
            table = self.query_one("#session-table", DataTable)
        except Exception:
            return
        # self.theme is kept in step with the system by _sync_system_theme
        # on every refresh; deriving `dark` from it here avoids the
        # `defaults read` subprocess _system_is_dark() runs, which has no
        # place on a 132ms main-thread tick.
        cell = render_status_cell("working", self._spin_idx, dark=(self.theme != "gruvbox-light"))
        for s in self._flat_rows:
            if s.status == "working":
                try:
                    table.update_cell(s.session_id, "status", cell)
                except Exception:
                    pass

    def _compose_first_cell(self, s: Session, base: str) -> str:
        b = self._bell.get(s.session_id)
        if b and not b["acked"] and time.time() - b["rang_at"] < BELL_DECAY_S:
            glyph = "●" if self._pulse_phase else "○"
            return f"[#fabd2f]{glyph}[/] " + base.lstrip()
        if s.session_id in self._pinned:
            return "[cyan]⊙[/] " + base.lstrip()
        if s.is_scheduled:
            return "[dim]↻[/] " + base.lstrip()
        return base

    def _ack_bell(self, sid: str) -> None:
        b = self._bell.get(sid)
        if b:
            b["acked"] = True

    def _tick_bell(self) -> None:
        if not self._bell:
            return
        self._pulse_phase = not self._pulse_phase
        now = time.time()
        try:
            table = self.query_one("#session-table", DataTable)
        except Exception:
            return
        col0 = self._visible_cols[0] if self._visible_cols else None
        if not col0:
            return
        for sid in list(self._bell):
            b = self._bell[sid]
            if now - b["rang_at"] >= BELL_DECAY_S:
                b["acked"] = True
            s = next((x for x in self._flat_rows if x.session_id == sid), None)
            base = self._first_cell_base.get(sid)
            if s is None or base is None:
                if b["acked"]:
                    self._bell.pop(sid, None)
                continue
            first = self._compose_first_cell(s, base)
            cells = self._last_rendered.get(sid)
            if cells:
                cells[0] = first
            display = self._with_selection_style(sid, [first])[0]
            try:
                table.update_cell(sid, col0, display)
            except Exception:
                pass
            if b["acked"]:
                self._bell.pop(sid, None)

    def _rebuild_table_columns(self) -> None:
        table = self.query_one("#session-table", DataTable)
        table.clear(columns=True)
        for col_key in self._visible_cols:
            info = ALL_COLUMNS.get(col_key, {})
            width = None
            if col_key == "session":
                width = max(20, self.size.width // 3)
            table.add_column(info.get("label", col_key), key=col_key, width=width)

    def _fit_session_column(self, table: DataTable | None = None) -> None:
        """Give the session column whatever width remains after the other
        (auto-sized) columns — it's the elastic column, so trailing slack
        collapses before titles truncate."""
        if "session" not in self._visible_cols:
            return
        if table is None:
            table = self.query_one("#session-table", DataTable)
        try:
            col = table.columns["session"]
        except KeyError:
            return
        others = sum(
            c.get_render_width(table)
            for k, c in table.columns.items()
            if getattr(k, "value", k) != "session"
        )
        pad = 2 * table.cell_padding
        col.width = max(20, self.size.width - others - pad)
        col.auto_width = False
        col.content_width = 0

    def on_resize(self) -> None:
        try:
            self._fit_session_column()
        except Exception:
            pass

    def _filter_sessions(self, sessions: list[Session]) -> list[Session]:
        if not self._filter:
            return sessions
        f = self._filter.lower()
        return [s for s in sessions if (
            f in s.title.lower() or f in s.project.lower()
            or f in s.cwd.lower() or f in s.status
            or f in s.model.lower()
        )]

    _refresh_pending: bool = False
    _refresh_queued: bool = False  # set when a refresh is requested mid-flight
    # Incremented at the very end of _refresh_apply, the one moment the UI
    # reflects a completed refresh. Tests wait on this (see
    # tests/helpers.py wait_for_refresh) instead of a fixed pilot.pause()
    # tick, which raced the worker-thread -> main-thread hop and produced
    # load-dependent false failures; two were hand-bisected on
    # 2026-08-18 alone. Not a behaviour change: nothing in the app reads it.
    _refresh_generation: int = 0

    def refresh_sessions(self) -> None:
        """Schedule a refresh — heavy work runs in a background thread."""
        if self._refresh_pending:
            self._refresh_queued = True  # run again once current worker finishes
            return
        self._refresh_pending = True
        # Snapshot UI state before background work
        table = self.query_one("#session-table", DataTable)
        cr = table.cursor_row
        if cr is not None and 0 <= cr < len(self._row_map):
            sel = self._row_map[cr]
            if sel:
                self._selected_key = sel.session_id
        self.run_worker(
            lambda: self._refresh_compute(),
            thread=True,
        )

    def _refresh_compute(self) -> None:
        """Background thread: parse, sort, filter — no UI access."""
        try:
            sessions = parse_sessions(
                include_archived=self.show_archived,
                include_subagents=self.show_subagents,
                pinned=self._pinned,
            )

            # Hide closed sessions unless "All" is toggled
            if not self.show_scheduled:
                sessions = dedupe_scheduled_sessions(sessions, self._pinned)

            # True group sizes, from the set as if hide_inactive_pins were off:
            # a group's real membership must never depend on which of its
            # members happen to be hidden right now, or hiding an inactive
            # pin silently demotes its still-visible, still-active groupmate
            # from a named header down into "ungrouped" (observed live
            # 2026-08-16: a 2-member group loses its inactive pin, its
            # active member drops to ungrouped, reads as the active pin
            # itself having vanished).
            # Computed in BOTH archive views. It used to be {} under
            # show_archived, on the theory that history mode hides nothing;
            # inbox mode hides in history mode too, so with the fallback to
            # visible counts a group whose working members were filtered
            # folded its lone READY row into "ungrouped" there, the same
            # 2026-08-16 demotion in the other view (caught by review,
            # 2026-08-18). The membership rule is the base filter's own.
            sessions_kept_regardless = [
                s for s in sessions
                if self.show_archived or s.status != "closed" or s.session_id in self._pinned
            ]
            true_group_sizes: dict[str, int] = {}
            for s in sessions_kept_regardless:
                key = _group_key(s.title)
                true_group_sizes[key] = true_group_sizes.get(key, 0) + 1

            if not self.show_archived:
                # Step 1: the base filter, unchanged from before
                # hide_inactive_pins existed. "archived" was never hidden
                # here, only "closed" (and only when unpinned) — that must
                # stay true no matter what the toggle below does, or an
                # unpinned archived session starts vanishing by default (a
                # real regression this shipped with once already, caught
                # before merge by widening this exact line instead of
                # layering step 2 on top of it).
                sessions = [
                    s for s in sessions
                    if s.status != "closed" or s.session_id in self._pinned
                ]
                # Step 2: hide_inactive_pins, layered on top, never replacing
                # step 1. "Inactive" is exactly render_row()'s own bold/dim
                # test (Max: "the same that makes a row not bold"): status in
                # INACTIVE_STATUSES. That's a snapshot of CURRENT render
                # state, not usage history — a pin resumed routinely (e.g.
                # config-MCPs) whose terminal happens to be closed at this
                # exact moment reads as inactive by this rule and gets
                # hidden when the toggle is on, the same as it renders dim
                # in the table right now. That is the literal, accepted
                # tradeoff of reusing the existing bold/dim test rather than
                # a separate liveness or usage-history check.
                if self.hide_inactive_pins:
                    sessions = [
                        s for s in sessions
                        if not (s.status in INACTIVE_STATUSES and s.session_id in self._pinned)
                    ]

            if self._hidden:
                sessions = [s for s in sessions if s.session_id not in self._hidden]

            # Auto-close terminal tabs for debriefed sessions
            cleaned = _poll_debrief_done_signals(sessions)

            filtered = self._filter_sessions(sessions)
            sorted_sessions = sort_sessions(filtered, self.sort_mode)

            flat: list[Session] = []
            for s in sorted_sessions:
                flat.append(s)
                if self.show_subagents and s.subagents:
                    flat.extend(s.subagents)

            # Override status for sessions being dismissed
            for s in flat:
                if s.session_id in self._dismissing_sessions:
                    s.status = self._dismissing_sessions[s.session_id]

            # Inbox mode is a DISPLAY filter, so it applies to `flat` (what
            # renders), never to `sessions` (what the app knows about). A
            # first cut filtered `sessions` and starved everything
            # downstream that has nothing to do with the screen: the bell
            # never saw a hidden session's APPROVE transition, the debrief
            # poller consumed and lost a hidden session's done-signal, the
            # stats bar read "0 working" beside the INBOX chip, and
            # jump-by-name could not find a working session (all caught by
            # review, 2026-08-18). Placed after the dismissing override so
            # a "debriefing"/"closing" row is judged on that state. Keep-set
            # (ACTIONABLE_STATUSES), not a hide-list: the inbox and the
            # next-actionable key must share one definition of "needs you",
            # or a status added later shows in one and not the other.
            if self.inbox_mode:
                flat = [s for s in flat if s.status in ACTIONABLE_STATUSES]

            # When grouping, stable-sort by group key (preserves within-group
            # sort from the earlier sort_mode pass). Singletons collapse into
            # one "ungrouped" bucket at the bottom.
            if self.show_groups:
                groups: dict[str, list[Session]] = {}
                for s in flat:
                    groups.setdefault(_group_key(s.title), []).append(s)
                # A key's TRUE size (true_group_sizes) decides the fold, not
                # its currently-visible count: a group hiding_inactive_pins
                # shrank to one member is still a real group, not a singleton.
                singles = [k for k, v in groups.items()
                           if true_group_sizes.get(k, len(v)) < 2]
                if singles:
                    ungrouped = groups.setdefault("ungrouped", [])
                    for k in singles:
                        if k != "ungrouped":
                            ungrouped.extend(groups.pop(k))
                ordered_keys = sorted(
                    groups, key=lambda k: (k == "ungrouped", k.lower())
                )
                flat = [s for k in ordered_keys for s in groups[k]]
                self._group_counts = {k: len(groups[k]) for k in ordered_keys}
            else:
                self._group_counts = {}

            disambiguate_titles(flat)

            # "Seen" READY rows (Max, 2026-08-16): read fresh from prefs each
            # cycle, not held in memory, since a jump from Ctrl+Shift+N runs
            # as a separate short-lived process and must be picked up here.
            # Read-only and in-memory from here: an earlier version also
            # pruned stale entries and wrote the result back, which raced
            # the read-modify-write in _mark_ready_seen() (called from a
            # jump that can land mid-refresh-cycle) and clobbered a
            # just-added seen mark back to unseen (Max, 2026-08-16: "my
            # 'marked as read' doesn't seem to stay that way"). _mark_ready_
            # seen() is now the only writer of acked_ready; a stale entry
            # for a session no longer done is simply never matched below
            # (done_now excludes it), so it's inert, not wrong, and costs
            # nothing worth a second writer to avoid.
            done_now = {s.session_id for s in flat if s.status == "done"}
            all_acked = _normalize_acked_ready(load_prefs().get("acked_ready", {}))
            acked_ready = {sid: v for sid, v in all_acked.items() if sid in done_now}

            # Check system appearance in background thread (avoids
            # subprocess.run on Textual's main thread). Computed before the
            # render loop below: the unseen-READY color depends on it
            # (Max, 2026-08-17: yellow doesn't pop against light mode's
            # own cream background, needs its own popping blue there).
            sys_dark = _system_is_dark()

            # Pre-render rows in background thread (Rich markup generation)
            visible_cols = self._visible_cols
            rendered = [(s, render_row(s, visible_cols, acked_ready=acked_ready, dark=sys_dark))
                       for s in flat]

            # Post to main thread for UI update
            self.call_from_thread(
                self._refresh_apply, sessions, flat, rendered, cleaned,
                sys_dark,
            )
        finally:
            # Bookkeeping hops to the main loop. It used to run here on
            # the worker thread while refresh_sessions() (main thread)
            # reads _refresh_pending and sets _refresh_queued: main could
            # read pending=True and set queued=True in the instant after
            # this thread had already checked queued and found it False,
            # leaving queued=True with no worker to drain it. That refresh
            # was silently lost until the next 3s tick, and once tests
            # waited on the flags it surfaced as a 5s settle() timeout
            # (review, 2026-08-19). On one thread the interleaving cannot
            # happen.
            self.call_from_thread(self._refresh_finished)

    def _refresh_finished(self) -> None:
        """Main thread: clear the in-flight flag and run a refresh that was
        requested while the worker was busy. Lives on the main loop so it
        can never interleave with refresh_sessions()'s own flag reads."""
        self._refresh_pending = False
        if self._refresh_queued:
            self._refresh_queued = False
            self.refresh_sessions()

    def _refresh_apply(self, sessions: list[Session], flat: list[Session],
                       rendered: list[tuple[Session, list[str]]],
                       cleaned: list[str],
                       sys_dark: bool = True) -> None:
        """Main thread: apply computed results to UI."""
        self._sync_system_theme(sys_dark)
        self.sessions = sessions

        # SHADOW: cross-row invariant audit on the real session set every refresh.
        # Constructs the would-be resolved identities and checks for dup keys /
        # instance-id collisions / key-sid mismatch, logging any violation as a
        # self-describing fixture. Observe-only; never affects rendering.
        try:
            _ris = [
                ResolvedIdentity(
                    key=(s.instance_id if (s.status != "closed" and s.instance_id)
                         else (s.sid or s.session_id)),
                    sid=(s.sid or s.session_id), instance_id=s.instance_id,
                    pid=None, started_ms=0, title=s.title, title_source="shadow",
                    status=s.status, alive=(s.status != "closed"),
                    cwd=s.cwd, origin="shadow", source="shadow",
                )
                for s in sessions if not s.is_subagent and (s.sid or s.session_id)
            ]
            audit_identities(_ris)
        except Exception:
            pass

        if cleaned:
            self.notify(
                f"Auto-closed {len(cleaned)} debriefed session{'s' if len(cleaned) > 1 else ''}",
                timeout=3,
            )

        # Log status transitions. Over `sessions` (everything the app
        # knows), NOT `flat` (what is on screen): with inbox mode on, a
        # session hidden while working must still seed and reset
        # _prev_statuses, or its APPROVE arrival rings no bell, and a
        # session that rang, got approved, went back to working (hidden)
        # and asked again reads as "no transition" (review, 2026-08-18).
        # An inbox that goes silent on exactly the events it exists to
        # surface would be worse than no inbox.
        for s in sessions:
            if s.is_subagent:
                continue
            prev = self._prev_statuses.get(s.session_id)
            if prev and prev != s.status:
                mlog("status", "transition", sid=s.session_id[:12],
                     title=s.title, prev=prev, new=s.status)
                if s.status == "needs_approval":
                    self._bell[s.session_id] = {"rang_at": time.time(), "acked": False}
            if s.status != "needs_approval":
                self._bell.pop(s.session_id, None)
            self._prev_statuses[s.session_id] = s.status

        table = self.query_one("#session-table", DataTable)
        # Snapshot cursor and scroll right before clear (user may have navigated
        # since refresh_sessions() dispatched the worker). Must read the OLD
        # _row_map here — cursor_row indexes the table as it was rendered.
        old_map = self._row_map
        cr = table.cursor_row
        selected_key = self._selected_key
        saved_row_idx = cr
        if cr is not None and 0 <= cr < len(old_map):
            sel = old_map[cr]
            if sel:
                selected_key = sel.session_id
                saved_row_idx = None  # will restore by key instead

        self._flat_rows = flat
        saved_scroll_x = table.scroll_x
        saved_scroll_y = table.scroll_y

        # Everything between clear() and cursor-restore can fire spurious
        # RowHighlighted events (e.g. for row 0 after re-add). Hold the guard
        # until those queued messages have drained.
        self._extending_cursor = True
        table.clear()
        self._last_rendered = {}
        self._first_cell_base = {}
        n_cols = len(self._visible_cols)
        last_group = None
        row_map: list[Session | None] = []
        group_header_rows: list[int] = []
        for s, cells in rendered:
            if self.show_groups:
                gk = _group_key(s.title)
                # Singletons were merged into "ungrouped" upstream, so any
                # session whose own key isn't a real group header must be
                # part of that bucket.
                if gk not in self._group_counts:
                    gk = "ungrouped"
                if gk != last_group:
                    if last_group is not None:
                        spacer = [""] * n_cols
                        table.add_row(*spacer, key=f"__spacer__{gk}")
                        row_map.append(None)
                    count = self._group_counts.get(gk, 1)
                    style = "dim" if gk == "ungrouped" else "bold cyan"
                    label = f"[{style}]▸ {gk}[/] [dim]({count})[/]"
                    header = [label] + [""] * (n_cols - 1)
                    group_header_rows.append(len(row_map))
                    table.add_row(*header, key=f"__group__{gk}")
                    row_map.append(None)
                    last_group = gk
                # Indent first cell so member rows nest under the ▸ header
                cells = ["  " + cells[0], *cells[1:]]
            self._first_cell_base[s.session_id] = cells[0]
            cells = [self._compose_first_cell(s, cells[0]), *cells[1:]]
            self._last_rendered[s.session_id] = cells
            display = self._with_selection_style(s.session_id, cells)
            table.add_row(*display, key=s.session_id)
            row_map.append(s)
        self._row_map = row_map
        self._group_header_rows = group_header_rows

        # scroll=False: we restore the user's scroll position explicitly below.
        # Default scroll=True would yank the viewport to the cursor row on
        # every refresh, fighting the scroll_to() and bouncing the user up.
        restored = False
        if saved_row_idx is None and selected_key:
            for idx, s in enumerate(row_map):
                if s and s.session_id == selected_key:
                    table.move_cursor(row=idx, scroll=False)
                    restored = True
                    break
        elif saved_row_idx is not None:
            table.move_cursor(row=min(saved_row_idx, len(row_map) - 1), scroll=False)
            restored = True
        if not restored:
            # The cursored session is no longer in the table (a toggle such
            # as inbox mode or hide_inactive_pins just filtered it, or a
            # search narrowed past it). Textual leaves the cursor at row 0,
            # which under grouping is a header (row_map[0] is None), so
            # Enter and n silently do nothing. Land on the first real row
            # instead. Lives here, not in each toggle's action: pre-parking
            # _selected_key in an action is dead code, since
            # refresh_sessions() and the snapshot above both re-derive the
            # key from the live cursor first (caught by review, 2026-08-18).
            first_real = next((i for i, s in enumerate(row_map) if s is not None), None)
            if first_real is not None:
                table.move_cursor(row=first_real, scroll=False)
        self.call_after_refresh(self._release_cursor_guard)

        self._fit_session_column(table)
        self.call_after_refresh(self._fit_session_column)

        table.scroll_to(saved_scroll_x, saved_scroll_y, animate=False)
        self.call_after_refresh(
            lambda: table.scroll_to(saved_scroll_x, saved_scroll_y, animate=False)
        )
        self.query_one(StatsBar).update_stats(
            self.sessions, self.sort_mode, dark=sys_dark, inbox_mode=self.inbox_mode)
        self._refresh_generation += 1

    def _make_menu_handler(self, s: Session):
        """Build the SessionMenu dismiss callback for a session."""
        def handle_action(action: str | None) -> None:
            mlog("menu", "action", action=action, sid=s.session_id[:12],
                 title=s.title, status=s.status)
            if action == "jump":
                self._ack_bell(s.session_id)
                ok = focus_terminal_session(s)
                if not ok:
                    # If the session's process is alive, its window exists
                    # SOMEWHERE — resuming would spawn a duplicate. Log the
                    # divergence and tell the user instead.
                    if _is_session_alive(s.session_id):
                        mlog("DIVERGE", "alive_but_unfound",
                             sid=s.session_id[:12], title=s.title,
                             candidates=_resolve_match_candidates(s))
                        # Heal stale hook state — find the real PID/TTY
                        _heal_hook_state(s.session_id)
                        self.notify(
                            f"Window not found for {s.title[:20]}. "
                            "Press Enter → Resume to open in a new tab.",
                            timeout=6, severity="warning",
                        )
                    else:
                        ok = resume_session(s)
                        if ok:
                            self.notify(f"Resuming {s.title[:20]} in new window", timeout=4)
                        else:
                            self.notify("Could not find or resume session", timeout=4)
                mlog("menu", "jump_result", sid=s.session_id[:12], success=ok)
                if ok:
                    _mark_ready_seen(s.session_id, s.status, s.last_activity)
                    self.action_clear_search()
            elif action == "edit_name":
                self.action_edit_name()
            elif action == "resume":
                ok = resume_session(s)
                if ok:
                    self.notify(f"Resuming {s.title[:20]}…", timeout=4)
                else:
                    self.notify("Could not open terminal", timeout=4)
                mlog("menu", "resume_result", sid=s.session_id[:12], success=ok)
                if ok:
                    self.action_clear_search()
            elif action == "copy_id":
                copy_to_clipboard(s.session_id)
                self.notify("Copied", timeout=3)
            elif action == "remote" and s.remote_url:
                subprocess.run(["open", s.remote_url], capture_output=True)
            elif action == "transcript":
                subprocess.run(["open", "-R", s.transcript_path], capture_output=True)
            elif action == "dismiss":
                self._start_dismiss(s)
            elif action == "kill":
                pid = _find_claude_pid(s)
                if pid:
                    self.run_worker(
                        lambda s=s, pid=pid: self._kill_and_close_tab(s, pid),
                        thread=True,
                    )
                else:
                    self.notify("No process found", timeout=4)
        return handle_action

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter key on a row — open context menu.
        (Mouse clicks no longer reach here; SessionTable.on_click owns them.)"""
        if not (event.row_key and event.row_key.value):
            return
        s = next((s for s in self._flat_rows if s.session_id == event.row_key.value), None)
        if not s:
            return
        self.push_screen(SessionMenu(s), self._make_menu_handler(s))

    def on_session_table_row_double_clicked(
        self, event: "SessionTable.RowDoubleClicked"
    ) -> None:
        if not event.row_key or not event.row_key.value:
            return
        s = next((s for s in self._flat_rows if s.session_id == event.row_key.value), None)
        if not s:
            return
        self._make_menu_handler(s)("jump")

    def _start_dismiss(self, session: Session) -> None:
        sid = session.session_id
        if sid in self._dismissing_sessions:
            mlog("dismiss", "already_in_progress", sid=sid[:12])
            return
        # Clear stale failure — let the user retry after fixes
        self._dismiss_failed.discard(sid)
        has_debrief = session_has_debrief(session.transcript_path)
        phase = "closing" if has_debrief else "debriefing"
        mlog("dismiss", "start", sid=sid[:12], title=session.title,
             has_debrief=has_debrief, phase=phase)
        self._dismissing_sessions[sid] = phase
        self.refresh_sessions()
        self.run_worker(
            lambda s=session, hd=has_debrief: self._dismiss_sync(s, hd),
            thread=True,
        )

    def _kill_and_close_tab(self, s: Session, pid: int) -> None:
        """Background worker: raise window, SIGTERM the process, then close
        the tab by typing `exit` once the shell reclaims the prompt.

        The window must be raised BEFORE killing — once the process exits,
        zsh shell-integration rewrites the title and the ·sid8 marker is
        gone, so the tab can't be found afterwards.
        """
        sid8 = s.session_id[:8]
        raised = _raise_window_by_content(s)
        front = _frontmost_terminal_title() if raised else ""
        front_ok = f"\u00b7{sid8}" in front

        try:
            os.kill(pid, signal.SIGTERM)
            mlog("menu", "kill", sid=s.session_id[:12], pid=pid,
                 raised=raised, front_ok=front_ok)
        except OSError as e:
            mlog("menu", "kill_error", sid=s.session_id[:12], error=str(e))
            self.call_from_thread(self.notify, f"Kill failed: {e}", timeout=4)
            return

        closed = False
        if front_ok:
            time.sleep(0.6)
            # Safety: a bare shell after the process exits has a cwd title
            # (no ·). Any · means a live session or the monitor — abort
            # rather than type `exit` into it.
            after = _frontmost_terminal_title()
            if "\u00b7" in (after or ""):
                mlog("DIVERGE", "kill_tab_close_abort", sid=s.session_id[:12],
                     front_before=front, front_after=after)
            else:
                try:
                    subprocess.run(
                        ["osascript", "-l", "JavaScript", "-e",
                         '(() => { const se = Application("System Events"); '
                         'se.keystroke("exit"); delay(0.05); se.keyCode(36); })()'],
                        capture_output=True, text=True, timeout=5,
                    )
                    closed = True
                    mlog("menu", "kill_tab_closed", sid=s.session_id[:12])
                except (subprocess.TimeoutExpired, OSError):
                    pass
        elif raised:
            mlog("DIVERGE", "kill_raise_wrong_front",
                 sid=s.session_id[:12], front=front)

        _raise_monitor_window()
        suffix = " + tab closed" if closed else ""
        self.call_from_thread(
            self.notify, f"Killed {s.title[:20]} (PID {pid}){suffix}", timeout=4,
        )
        self.call_from_thread(self.refresh_sessions)

    def _dismiss_sync(self, session: Session, has_debrief: bool) -> None:
        """Background worker: debrief (if needed), wait for exit, close tab."""
        sid = session.session_id

        if has_debrief:
            pid = _find_claude_pid(session)
            mlog("dismiss", "kill_existing", sid=sid[:12], pid=pid)
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError as e:
                    mlog("dismiss", "kill_error", sid=sid[:12], pid=pid, error=str(e))
        else:
            sent = _send_to_terminal_session(session, "/debrief")
            mlog("dismiss", "send_debrief", sid=sid[:12], success=sent)
            if not sent:
                self._dismiss_failed.add(sid)
                self.call_from_thread(
                    self.notify, "Could not find terminal to debrief", timeout=3,
                )
                self._dismissing_sessions.pop(sid, None)
                self.call_from_thread(self.refresh_sessions)
                return

        # Poll until the Claude process exits (10 min timeout)
        # Cache the PID to avoid re-running lsof/ps every poll
        poll_count = 0
        max_polls = 200  # 200 * 3s = 10 minutes
        cached_pid = _find_claude_pid(session)
        while poll_count < max_polls:
            if cached_pid:
                try:
                    os.kill(cached_pid, 0)
                except OSError:
                    break  # Process exited
            elif _find_claude_pid(session) is None:
                break
            poll_count += 1
            if poll_count % 10 == 0:  # Log every 30s
                mlog("dismiss", "waiting_for_exit", sid=sid[:12], polls=poll_count)
            time.sleep(3)

        if poll_count >= max_polls:
            mlog("dismiss", "timeout", sid=sid[:12])
            self._dismissing_sessions.pop(sid, None)
            self.call_from_thread(self.notify, "Debrief timed out", timeout=5)
            self.call_from_thread(self.refresh_sessions)
            return

        mlog("dismiss", "process_exited", sid=sid[:12], polls=poll_count)

        # Close the terminal tab
        self._dismissing_sessions[sid] = "closing"
        self.call_from_thread(self.refresh_sessions)
        time.sleep(1)
        closed = _close_terminal_tab(session)
        mlog("dismiss", "tab_closed", sid=sid[:12], success=closed)

        self._dismissing_sessions.pop(sid, None)
        title = session.title[:20]
        self.call_from_thread(self.notify, f"Debriefed {title}", timeout=4)
        self.call_from_thread(self.refresh_sessions)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Cursor moves clear multi-selection unless landing inside it (extend),
        # and always disarm a pending delete-confirm if the target shifted.
        new_sid = event.row_key.value if (event.row_key and event.row_key.value) else None
        if not self._extending_cursor:
            if self._selection and new_sid not in self._selection:
                self._set_selection(set())
                self._selection_anchor = None
            if self._delete_armed_for is not None:
                want = self._selection or ({new_sid} if new_sid else set())
                if not want or not self._delete_armed_for.issuperset(want):
                    self._delete_armed_for = None
        if not (event.row_key and event.row_key.value):
            return
        key = event.row_key.value
        # Skip spacer rows — move in the same direction the user was going
        if key.startswith("__spacer__"):
            table = self.query_one("#session-table", DataTable)
            cr = table.cursor_row
            if cr is not None:
                direction = 1 if cr >= self._last_cursor_row else -1
                target = cr + direction
                if 0 <= target < table.row_count:
                    table.move_cursor(row=target)
            return
        # Group headers are valid for PgUp/PgDn navigation
        if key.startswith("__group__"):
            self._last_cursor_row = table.cursor_row or 0 if (table := self.query_one("#session-table", DataTable)) else 0
            return
        s = next((s for s in self._flat_rows if s.session_id == key), None)
        if not s:
            return
        table = self.query_one("#session-table", DataTable)
        self._last_cursor_row = table.cursor_row or 0

        icon, color = STATUS_DISPLAY.get(s.status, ("?", "white"))
        header = f"[bold]{s.title}[/] [{color}]{icon}[/]"

        # Build detail content: archived summary, plan, or last assistant text
        detail_parts = [header]

        if s.status in INACTIVE_STATUSES:
            detail_parts.append(
                f"[dim]Project:[/] {s.project}  "
                f"[dim]Cost:[/] ${s.cost:.2f}  "
                f"[dim]Output:[/] {format_tokens(s.tokens_out)}  "
                f"[dim]Messages:[/] {s.message_count}"
            )
            detail_parts.append("[dim]Press Enter → Resume to continue this session[/]")

        tasks = load_tasks(s.session_id)
        body = None
        if tasks:
            detail_parts.append(format_plan(tasks))
        elif s.last_assistant_text:
            from rich.markdown import Markdown
            body = Markdown(s.last_assistant_text[:1200],
                            code_theme="monokai", hyperlinks=False)

        if not _get_api_key():
            detail_parts.append("[dim #D97757]Press Shift+K to add API key for haiku session summaries[/]")

        from rich.console import Group
        from rich.text import Text
        head = Text.from_markup("\n".join(detail_parts))
        self.query_one("#detail-panel", Static).update(
            Group(head, body) if body else head
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-bar":
            self._filter = event.value
            self._selected_key = None
            self.refresh_sessions()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-bar":
            self._dismiss_search()

    def action_start_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        search_bar.display = True
        self.query_one("#search-hint", Label).display = False
        search_bar.focus()

    def action_clear_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        if search_bar.display or self._filter:
            self._dismiss_search()

    def action_search_to_table(self) -> None:
        # Down while typing in the search box: keep the filter, drop focus into
        # the (filtered) table so hotkeys work on the matched rows. DataTable
        # owns Down for cursor movement, so this only fires from the Input.
        search_bar = self.query_one("#search-bar", Input)
        if not search_bar.display:
            return
        self.query_one("#session-table", DataTable).focus()

    def _dismiss_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        search_bar.value = ""
        search_bar.display = False
        self.query_one("#search-hint", Label).display = True
        self._filter = ""
        self.refresh_sessions()
        self.query_one("#session-table", DataTable).focus()

    def action_cycle_sort(self) -> None:
        self.sort_mode = self.sort_mode.next()
        self._selected_key = None
        self.refresh_sessions()
        self.notify(f"Sort: {self.sort_mode.label}", timeout=3)

    def action_toggle_subagents(self) -> None:
        self.show_subagents = not self.show_subagents
        self.refresh_sessions()
        self.notify(f"Subagents {'shown' if self.show_subagents else 'hidden'}", timeout=3)

    # ── Multi-select & hide (history mode) ────────────────────────────────

    def _cursor_session(self) -> "Session | None":
        table = self.query_one("#session-table", DataTable)
        cr = table.cursor_row
        if cr is None or not (0 <= cr < len(self._row_map)):
            return None
        return self._row_map[cr]

    def _row_index_of(self, sid: str) -> int | None:
        for i, s in enumerate(self._row_map):
            if s and s.session_id == sid:
                return i
        return None

    _SELECTION_BG = "on rgb(58,58,80)"

    def _release_cursor_guard(self) -> None:
        self._extending_cursor = False

    def _with_selection_style(self, sid: str, cells: list[str]) -> list[str]:
        if sid not in self._selection:
            return cells
        return [f"[{self._SELECTION_BG}]{c}[/]" for c in cells]

    def _set_selection(self, sids: set[str]) -> None:
        if sids == self._selection:
            return
        old = self._selection
        self._selection = sids
        self._delete_armed_for = None
        table = self.query_one("#session-table", DataTable)
        cols = self._visible_cols
        for sid in old ^ sids:
            base = self._last_rendered.get(sid)
            if not base:
                continue
            display = self._with_selection_style(sid, base)
            for col, val in zip(cols, display):
                try:
                    table.update_cell(sid, col, val)
                except Exception:
                    pass

    def action_extend_selection(self, delta: int) -> None:
        table = self.query_one("#session-table", DataTable)
        cur = self._cursor_session()
        if cur is None:
            return
        if not self._selection or self._selection_anchor is None:
            self._selection_anchor = cur.session_id
        # Move cursor by delta, skipping non-session rows (group headers/spacers)
        cr = table.cursor_row or 0
        n = len(self._row_map)
        nr = cr
        while True:
            nr += delta
            if not (0 <= nr < n):
                nr = max(0, min(n - 1, nr))
                break
            if self._row_map[nr] is not None:
                break
        # Selection = all session rows between anchor and cursor (inclusive).
        # Set BEFORE moving the cursor so the RowHighlighted handler sees the
        # new row already inside the selection and leaves it alone.
        ai = self._row_index_of(self._selection_anchor)
        if ai is None:
            ai = nr
        lo, hi = sorted((ai, nr))
        sids = {s.session_id for s in self._row_map[lo:hi + 1] if s}
        self._set_selection(sids)
        table.move_cursor(row=nr)

    def action_toggle_pin(self) -> None:
        cur = self._cursor_session()
        if cur is None:
            return
        sid = cur.session_id
        if sid in self._pinned:
            self._pinned.discard(sid)
            self.notify(f"Unpinned {cur.title}", timeout=2)
        else:
            self._pinned.add(sid)
            self._hidden.discard(sid)
            self.notify(f"Pinned {cur.title} — stays visible when closed", timeout=3)
        save_pinned_sessions(self._pinned)
        if sid not in self._pinned and sid in self._hidden:
            save_hidden_sessions(self._hidden)
        self.refresh_sessions()

    def action_hide_selected(self) -> None:
        target: set[str] = set(self._selection)
        if not target:
            cur = self._cursor_session()
            if cur:
                target = {cur.session_id}
        # Restrict to archived/closed — never hide a live session by accident
        eligible = {
            s.session_id for s in self._flat_rows
            if s.session_id in target and s.status in INACTIVE_STATUSES
        }
        if not eligible:
            self.notify("Nothing hideable under cursor (archived/closed only)",
                        severity="warning", timeout=3)
            return
        if self._delete_armed_for == frozenset(eligible):
            self._hidden |= eligible
            save_hidden_sessions(self._hidden)
            n = len(eligible)
            self.notify(f"Hidden {n} session{'s' if n != 1 else ''}", timeout=3)
            self._selection = set()
            self._selection_anchor = None
            self._delete_armed_for = None
            # Land the cursor on the nearest surviving row so refresh restores
            # by that key instead of the just-hidden one (which would reset to 0).
            table = self.query_one("#session-table", DataTable)
            cr = table.cursor_row or 0
            survivor = None
            for i in range(cr, len(self._row_map)):
                s = self._row_map[i]
                if s and s.session_id not in self._hidden:
                    survivor = i
                    break
            if survivor is None:
                for i in range(cr - 1, -1, -1):
                    s = self._row_map[i]
                    if s and s.session_id not in self._hidden:
                        survivor = i
                        break
            if survivor is not None:
                self._extending_cursor = True
                try:
                    table.move_cursor(row=survivor)
                finally:
                    self._extending_cursor = False
            self.refresh_sessions()
        else:
            self._delete_armed_for = frozenset(eligible)
            n = len(eligible)
            what = "this session" if n == 1 else f"{n} sessions"
            self.notify(f"Press delete again to hide {what}",
                        severity="warning", timeout=4)

    def on_session_table_row_shift_clicked(
        self, event: "SessionTable.RowShiftClicked"
    ) -> None:
        table = self.query_one("#session-table", DataTable)
        if self._selection_anchor is None:
            cur = self._cursor_session()
            self._selection_anchor = cur.session_id if cur else None
        ai = self._row_index_of(self._selection_anchor) if self._selection_anchor else None
        if ai is None:
            ai = table.cursor_row or 0
        lo, hi = sorted((ai, event.row_index))
        sids = {s.session_id for s in self._row_map[lo:hi + 1] if s}
        self._set_selection(sids)
        table.move_cursor(row=event.row_index)

    def action_jump_next_actionable(self) -> None:
        """Plain "n": move the cursor to the next session that needs you,
        walking down the table in the order you're actually looking at it,
        nothing more. It does NOT jump: Ctrl+Shift+N is the actual jump
        command, from inside the monitor or anywhere else on the machine.
        n-Enter-Enter (move, open the menu, pick Jump) jumps too, naked n
        does not (Max, 2026-08-16: "n in monitor should just move the
        highlighted row"). Doesn't touch next_cursor (that's the headless
        path's own walk-the-queue position, and pure browsing shouldn't
        silently advance it) or the READY read/unread mark (that's earned
        by actually jumping, not by looking). Uses
        _next_actionable_in_table_order, not find_next_actionable: the
        latter cycles by urgency (needs_approval, then oldest-waiting),
        which can jump past a row you can see is READY to reach an older
        one further down the table (Max, 2026-08-17: "naked n just skipped
        a READY claude row"). Picks from self._flat_rows (the
        search-filtered, currently-displayed set), not self.sessions
        (unfiltered): candidates hidden by an active search aren't
        reachable rows to move to, and self.sessions has no bearing on
        what's actually in the table right now."""
        current = self._cursor_session()
        current_index = None
        if current is not None:
            current_index = next(
                (i for i, s in enumerate(self._flat_rows) if s.session_id == current.session_id),
                None,
            )
        target = _next_actionable_in_table_order(self._flat_rows, current_index)
        if target is None:
            self.notify("Nothing needs you right now", timeout=3)
            return
        row = self._row_index_of(target.session_id)
        if row is not None:
            self.query_one("#session-table", DataTable).move_cursor(row=row)

    def action_toggle_archived(self) -> None:
        self._selection = set()
        self._selection_anchor = None
        self._delete_armed_for = None
        self.show_archived = not self.show_archived
        self.refresh_sessions()
        self.notify(f"All sessions {'shown' if self.show_archived else 'recent only'}", timeout=3)

    def action_save_layout(self) -> None:
        """Ctrl+L: snapshot every Ghostty window and tab, pin every Claude
        in them, and write monitor-layout.json, so Ghostty can be closed
        and `claude-monitor --restore-layout` rebuilds it (Max,
        2026-08-23). Runs on a worker: the AX walk and parse_sessions are
        both slow for the main thread."""
        sessions = list(self.sessions)  # main-thread snapshot; no parse on the worker

        def work():
            result = save_layout(sessions=sessions)
            r = result["summary"]

            def apply():
                if not result.get("ok"):
                    self.notify(f"Layout NOT saved: {result.get('reason', 'unknown')}",
                                timeout=8, severity="error")
                    return
                # Reload pins into memory: action_toggle_pin writes
                # self._pinned back to disk wholesale, so a stale in-memory
                # set would silently undo the pins the save just added on
                # the next Ctrl+P.
                self._pinned = load_pinned_sessions()
                self.notify(
                    f"Layout saved: {r['windows']} windows, {r['claudes']} Claudes "
                    f"({r['newly_pinned']} newly pinned). Restore: claude-monitor --restore-layout",
                    timeout=8,
                )
                self.refresh_sessions()
            self.call_from_thread(apply)
        self.run_worker(work, thread=True)

    def action_toggle_inbox_mode(self) -> None:
        """Ctrl+B: hide every working and standby row so only what needs
        you is left (Max, 2026-08-18: "an inbox mode ... so that I can
        further hone my attention focus"). Ctrl+I was the ask, but Ctrl+I
        and Tab are the same byte to Textual's terminal driver, so a
        binding on it is unreachable (the same wall the detail panel hit
        on 2026-08-15); B is for inBox. If the cursored row is hidden by
        the toggle, _refresh_apply's restore falls back to the first real
        row (that fallback lives there so it serves every filter, not just
        this one)."""
        self._selection = set()
        self._selection_anchor = None
        self._delete_armed_for = None
        turning_on = not self.inbox_mode
        self.inbox_mode = turning_on
        self._save_view_state()
        self.refresh_sessions()
        self.notify("Inbox: only what needs you" if turning_on else "Inbox off", timeout=3)

    def action_toggle_hide_inactive_pins(self) -> None:
        """Pins never expire on their own (Max: 'pins should stay until I
        unpin them'). This is the simpler ask instead: an on/off lens over
        the default view, not a decay timer. Off shows every pin regardless
        of age or status, same as before pins existed at all; on hides a
        pinned-but-closed session until you unpin it or open history mode."""
        self.hide_inactive_pins = not self.hide_inactive_pins
        self.refresh_sessions()
        self.notify(
            f"Inactive pins {'hidden' if self.hide_inactive_pins else 'shown'}",
            timeout=3,
        )

    def action_toggle_groups(self) -> None:
        self.show_groups = not self.show_groups
        self.refresh_sessions()
        self.notify(f"Grouping {'on' if self.show_groups else 'off'}", timeout=3)

    def action_toggle_detail(self) -> None:
        self.show_detail = not self.show_detail
        self.query_one("#detail-panel", Static).display = self.show_detail
        self._save_view_state()
        self.notify(f"Preview {'on' if self.show_detail else 'off'}", timeout=2)

    def action_send_prompt(self) -> None:
        """Space: type a prompt and send it to the session under the cursor,
        staying in the monitor afterwards (Max, 2026-08-28). The send reuses
        the same path as rename and the /proactive broadcast, so it inherits
        their refusal to type when the session's marker is ambiguous: typing
        a prompt at the wrong Claude is unrecoverable."""
        s = self._cursor_session()
        if s is None:
            return
        if s.status in INACTIVE_STATUSES or not _is_session_alive(s.session_id):
            self.notify("That session is not running", timeout=3)
            return

        def on_submit(text: str | None) -> None:
            if not text:
                return
            self.notify(f"Sending to {s.title}…", timeout=2)

            def work(sess=s, msg=text):
                ok = _send_to_terminal_session(sess, msg, return_to_monitor=True)
                mlog("action", "send_prompt", sid=sess.session_id[:12],
                     title=sess.title, chars=len(msg), ok=ok)
                self.call_from_thread(
                    self.notify,
                    f"Sent to {sess.title}" if ok else
                    f"Could not reach {sess.title}'s terminal",
                    timeout=4 if ok else 6,
                    severity="information" if ok else "warning",
                )
                if ok:
                    self.call_from_thread(self.refresh_sessions)
            # A send raises a window and types: seconds of JXA, never on the
            # main thread or the whole TUI freezes mid-send.
            self.run_worker(work, thread=True)

        self.push_screen(PromptSend(s.title), on_submit)

    def action_edit_name(self) -> None:
        """Open inline prompt, then send /rename <name> to the selected session."""
        table = self.query_one(DataTable)
        cr = table.cursor_row
        if cr is None or cr >= len(self._row_map):
            return
        s = self._row_map[cr]
        if (s is None
                or s.status in INACTIVE_STATUSES
                or not _is_session_alive(s.session_id)):
            self.notify("Rename requires a running session", timeout=3)
            return

        def on_submit(name: str | None) -> None:
            if not name:
                return
            ok = _send_to_terminal_session(s, f"/rename {name}", return_to_monitor=True)
            if ok:
                # Optimistically update the hook state so the monitor shows
                # the new name immediately (before the hook catches up).
                state_path = Path.home() / ".claude" / "session-states" / f"{s.session_id}.json"
                try:
                    data = json.loads(state_path.read_text()) if state_path.exists() else {}
                    data["title"] = name
                    data["title_source"] = "user"
                    data["title_updated_at"] = datetime.now().isoformat()
                    state_path.write_text(json.dumps(data, indent=2) + "\n")
                    _hook_state_cache.pop(s.session_id, None)
                except (OSError, json.JSONDecodeError):
                    pass
                # Also update the statusline name file so jump candidates stay in sync
                try:
                    Path(f"/tmp/claude-name-{s.session_id}").write_text(name)
                except OSError:
                    pass
                self.notify(f"Renamed → {name}", timeout=3)
                self.refresh_sessions()
            else:
                self.notify("Could not reach session terminal", timeout=4,
                            severity="warning")
            mlog("action", "edit_name", sid=s.session_id[:12], name=name, ok=ok)

        self.push_screen(RenamePrompt(s.title), on_submit)

    def _do_rename(self, s: Session, log_cat: str) -> None:
        ok = _send_to_terminal_session(s, "/rename")
        if ok:
            self.notify(f"Sent /rename to {s.title[:20]}", timeout=3)
        else:
            ok = resume_session(s)
            if ok:
                self.notify(f"Resuming {s.title[:20]} in new window", timeout=4)
            else:
                self.notify("Could not find or resume session", timeout=4)
        mlog(log_cat, "rename", sid=s.session_id[:12], success=ok)

    def _resolve_cursor_group(self) -> tuple[str, list[Session]]:
        """Return (group_key, live_sessions) for the row under the cursor.

        Works whether the cursor is on a group header or a session row.
        Only returns sessions whose process is alive.
        """
        table = self.query_one(DataTable)
        cr = table.cursor_row
        if cr is None or not (0 <= cr < len(self._row_map)):
            return "", []
        sel = self._row_map[cr]
        if sel is None:
            # Group header — first session after it belongs to this group
            for i in range(cr + 1, len(self._row_map)):
                if self._row_map[i] is not None:
                    sel = self._row_map[i]
                    break
            if sel is None:
                return "", []
        gk = _group_key(sel.title)
        if gk not in self._group_counts:
            gk = "ungrouped"
        members = [
            s for s in self._flat_rows
            if not s.is_subagent
            and s.status not in INACTIVE_STATUSES
            and (_group_key(s.title) == gk
                 or (gk == "ungrouped" and _group_key(s.title) not in self._group_counts))
            and _is_session_alive(s.session_id)
        ]
        return gk, members

    def action_proactive_group(self) -> None:
        """Send /proactive to every live session in the cursor's group."""
        gk, members = self._resolve_cursor_group()
        if not members:
            self.notify("No live sessions in group", timeout=3)
            return
        if gk == "ungrouped":
            self.notify("Cursor is in 'ungrouped' — pick a named group",
                        timeout=4, severity="warning")
            return
        self.notify(f"Sending /proactive to {len(members)} in '{gk}'…", timeout=3)
        self.run_worker(
            lambda m=members, g=gk: self._broadcast_command(m, "/proactive", g),
            thread=True,
        )

    def _broadcast_command(self, sessions: list[Session], cmd: str, group: str) -> None:
        sent = 0
        for s in sessions:
            ok = _send_to_terminal_session(s, cmd)
            mlog("broadcast", "send", group=group, sid=s.session_id[:12],
                 title=s.title, cmd=cmd, ok=ok)
            if ok:
                sent += 1
            time.sleep(0.3)
        _raise_monitor_window()
        self.call_from_thread(
            self.notify,
            f"Sent {cmd} to {sent}/{len(sessions)} in '{group}'",
            timeout=5,
        )

    def action_rename_selected(self) -> None:
        """Send /rename to the currently selected session's terminal."""
        table = self.query_one(DataTable)
        cr = table.cursor_row
        if cr is None or cr >= len(self._row_map):
            return
        s = self._row_map[cr]
        if s is None:
            return
        if s.status in INACTIVE_STATUSES:
            self.notify("Session not running", timeout=3)
            return
        self._do_rename(s, "action")

    def _save_view_state(self) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ:
            return
        view_state = {
            "sort_mode": self.sort_mode.value,
            "show_subagents": self.show_subagents,
            "show_archived": self.show_archived,
            "show_groups": self.show_groups,
            "show_detail": self.show_detail,
            "hide_inactive_pins": self.hide_inactive_pins,
            "inbox_mode": self.inbox_mode,
        }
        _update_prefs(lambda p: p.__setitem__("view_state", view_state) or True)

    def _load_view_state(self) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ:
            return
        vs = load_prefs().get("view_state") or {}
        if not vs:
            return
        try:
            self.sort_mode = SortMode(vs.get("sort_mode", self.sort_mode.value))
        except ValueError:
            pass
        self.show_subagents = bool(vs.get("show_subagents", self.show_subagents))
        self.show_archived = bool(vs.get("show_archived", self.show_archived))
        self.show_groups = bool(vs.get("show_groups", self.show_groups))
        self.show_detail = bool(vs.get("show_detail", self.show_detail))
        self.hide_inactive_pins = bool(vs.get("hide_inactive_pins", self.hide_inactive_pins))
        self.inbox_mode = bool(vs.get("inbox_mode", self.inbox_mode))
        if not self.show_detail:
            try:
                self.query_one("#detail-panel", Static).display = False
            except Exception:
                pass

    def action_restart(self) -> None:
        self._save_view_state()
        # Refresh (the plain, in-process kind) is gone: R is the one
        # refresh key now (Max, 2026-08-18: "let's just replace normal
        # refresh with shift r, we don't need the cruft"). A restart is
        # also a deliberate "start over" moment, so clear every READY
        # seen-mark on the way: whatever's still done goes back to
        # unseen/highlighted rather than carrying forward what you'd
        # already decided to defer (Max, 2026-08-18: "shift r should
        # reset ready claudes to the blue highlighted").
        def _clear(prefs: dict) -> bool:
            if not prefs.get("acked_ready"):
                return False
            prefs["acked_ready"] = {}
            return True

        _update_prefs(_clear)
        # Best-effort pull to pick up code changes. Must never crash the app:
        # the repo dir can be missing (e.g. a worktree was removed out from under
        # a running instance), git may be off PATH, offline, or time out. Any of
        # those should still let the restart proceed, not throw an unhandled
        # FileNotFoundError/SubprocessError and take the whole TUI down.
        try:
            subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=_REPO_DIR, capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as e:
            try:
                from monitor_log import log as _restart_log
                _restart_log("error", "restart_git_pull_failed", err=str(e))
            except Exception:
                pass
        self.exit(return_code=RESTART_EXIT_CODE)

    async def action_quit(self) -> None:
        self._save_view_state()
        self.exit()

    def _sync_system_theme(self, sys_dark: bool) -> None:
        """Always follow macOS appearance."""
        want = "gruvbox-dark" if sys_dark else "gruvbox-light"
        if self.theme != want:
            self.theme = want

    def _group_header_indices(self) -> list[int]:
        """Return row indices of group header rows (not spacers)."""
        return list(self._group_header_rows)

    _TYPEAHEAD_TIMEOUT_S = 1.2

    def _typeahead_anchors(self) -> list[tuple[str, int]]:
        """(label, row_index) pairs, in display order, for the plain-letter
        jump: one entry per group. Grouped view lands on the header row;
        ungrouped has no headers, so it lands on the group's first row."""
        if self.show_groups and self._group_header_rows:
            return list(zip(self._group_counts.keys(), self._group_header_rows))
        seen: dict[str, int] = {}
        for i, s in enumerate(self._row_map):
            if s is None or s.is_subagent:
                continue
            seen.setdefault(_group_key(s.title), i)
        return list(seen.items())

    def on_key(self, event: events.Key) -> None:
        """A bare letter, typed in sequence like Finder/Explorer type-ahead
        find, jumps the cursor to the first group whose name starts with
        the typed letters: s, t, r, a jumps to 'strategy'. Every hotkey
        now requires Ctrl (see BINDINGS), so a plain letter never collides
        with one; the three Shift-bound hotkeys (K/R/P) are matched by
        Bindings before this handler ever sees them."""
        key = event.key
        if len(key) != 1 or not key.isalpha():
            return
        if len(self.screen_stack) > 1 or isinstance(self.focused, Input):
            return
        event.stop()
        letter = key
        now = time.time()
        if now - self._typeahead_last_key > self._TYPEAHEAD_TIMEOUT_S:
            self._typeahead_buffer = ""
        self._typeahead_last_key = now
        anchors = self._typeahead_anchors()
        if not anchors:
            return
        candidate = self._typeahead_buffer + letter
        idx = next((i for lbl, i in anchors if lbl.lower().startswith(candidate.lower())), None)
        if idx is None:
            candidate = letter
            idx = next((i for lbl, i in anchors if lbl.lower().startswith(candidate.lower())), None)
        if idx is None:
            return
        self._typeahead_buffer = candidate
        self.query_one("#session-table", DataTable).move_cursor(row=idx)

    def action_prev_group(self) -> None:
        if not self.show_groups:
            return
        table = self.query_one("#session-table", DataTable)
        cur = table.cursor_row or 0
        headers = self._group_header_indices()
        prev = [i for i in headers if i < cur]
        if prev:
            table.move_cursor(row=prev[-1])
        elif headers:
            table.move_cursor(row=headers[-1])

    def action_next_group(self) -> None:
        if not self.show_groups:
            return
        table = self.query_one("#session-table", DataTable)
        cur = table.cursor_row or 0
        headers = self._group_header_indices()
        nxt = [i for i in headers if i > cur]
        if nxt:
            table.move_cursor(row=nxt[0])
        elif headers:
            table.move_cursor(row=headers[0])

    def action_table_home(self) -> None:
        table = self.query_one("#session-table", DataTable)
        table.move_cursor(row=0)

    def action_table_end(self) -> None:
        table = self.query_one("#session-table", DataTable)
        table.move_cursor(row=table.row_count - 1)

    def action_pick_columns(self) -> None:
        picker = ColumnPicker(self._visible_cols, self._col_order)

        def on_dismiss(cols: list[str] | None) -> None:
            if cols is not None and cols:
                self._col_order = picker._col_keys
                self._visible_cols = cols
                def _cols(prefs: dict, cols=cols, order=self._col_order) -> bool:
                    prefs["columns"] = cols
                    prefs["column_order"] = order
                    return True

                _update_prefs(_cols)
                self._rebuild_table_columns()
                self.refresh_sessions()
                self.notify("Columns updated", timeout=3)

        self.push_screen(picker, on_dismiss)

    def action_statusline_config(self) -> None:
        sl_prefs = load_statusline_prefs()
        screen = StatuslineConfig(sl_prefs)

        def on_dismiss(result: dict[str, bool] | None) -> None:
            if result is not None:
                _update_prefs(lambda p: p.__setitem__("statusline", result) or True)
                changed = {k: v for k, v in result.items() if v != sl_prefs.get(k)}
                mlog("config", "statusline_saved", changed=changed)
                self.notify("Statusline config saved", timeout=3)

        self.push_screen(screen, on_dismiss)

    def action_toggle_debug(self) -> None:
        self.debug_logging = not self.debug_logging
        monitor_log.enabled = self.debug_logging
        state = "ON" if self.debug_logging else "OFF"
        # Log the toggle itself (even when turning off, so the log shows it)
        monitor_log.enabled = True
        mlog("app", "debug_toggled", state=state)
        monitor_log.enabled = self.debug_logging
        self.notify(f"Debug logging {state}", timeout=3)

    def _audit_stats(self) -> None:
        """Periodic snapshot of session status breakdown."""
        sessions = [s for s in self.sessions if not s.is_subagent]
        by_status: dict[str, int] = {}
        for s in sessions:
            by_status[s.status] = by_status.get(s.status, 0) + 1
        total_cost = sum(s.cost for s in sessions)
        mlog("audit", "stats", total=len(sessions),
             cost=f"${total_cost:.2f}", breakdown=by_status)

    def action_cursor_down(self) -> None:
        self.query_one("#session-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#session-table", DataTable).action_cursor_up()


def dedupe_scheduled_sessions(sessions: list["Session"], pinned: "set[str]") -> list["Session"]:
    """Scheduled runs (sdk-cli, headless): keep only the most-recent per
    (cwd, title) so daily scouts don't flood the view. The latest still
    shows so you can read the most recent run. Shared by the TUI's own
    refresh and jump_to_next_actionable() so the headless --jump-next CLI
    sees the exact same candidate set as the in-app "n" key: they merged
    into one reimplementation once, letting the two pick different targets
    for a shared next_cursor (caught 2026-08-16, before it shipped)."""
    latest: dict[tuple[str, str], Session] = {}
    kept = []
    for s in sessions:
        if not s.is_scheduled or s.session_id in pinned:
            kept.append(s)
            continue
        key = (s.cwd, s.title)
        cur = latest.get(key)
        if cur is None or s.last_activity > cur.last_activity:
            latest[key] = s
    kept.extend(latest.values())
    return kept


# "Needs you": needs_approval (blocking, explicit) and done (finished,
# waiting on your next move). Shared by the in-app "n" key and the headless
# --jump-next CLI path so pressing n inside the monitor and Ctrl+Shift+N from
# anywhere else are the same feature, not two independent reimplementations.
ACTIONABLE_STATUSES = ("needs_approval", "done")


def find_next_actionable(sessions: list["Session"], after_sid: str | None,
                         acked_ready: "dict[str, list] | set[str] | None" = None
                         ) -> "Session | None":
    """The next session that needs you, cycling from after_sid so repeated
    calls walk the whole queue instead of landing on the same one every
    time. needs_approval sorts before done (it's the one actually blocking);
    within each, the longest-waiting session comes first. For Ctrl+Shift+N
    (jump_to_next_actionable / _handle_jump_next_request): there's no
    visible cursor to reason about there, so priority-by-urgency is exactly
    right. NOT used for the "n" key inside the monitor; see
    _next_actionable_in_table_order for why that needs a different order.

    acked_ready excludes already-seen done sessions from the candidate
    pool entirely (Max, 2026-08-17: "I don't want ctrl-shift-n to jump me
    to an already read, redundantly... it keeps jumping me to X and I
    don't need it, I already jumped and took no action"). needs_approval
    is never excluded this way: acked_ready only ever holds done sessions
    (_mark_ready_seen is a no-op for any other status), and a blocking
    approval request stays urgent regardless of whether you've looked at
    it. If everything actionable happens to be already-seen, this
    correctly returns None rather than re-visiting one anyway."""
    actionable = [
        s for s in sessions
        if s.status in ACTIONABLE_STATUSES and not s.is_subagent
        and _effective_seen_count(acked_ready, s) == 0
    ]
    if not actionable:
        return None
    actionable.sort(key=lambda s: (0 if s.status == "needs_approval" else 1, s.last_activity))
    if after_sid:
        ids = [s.session_id for s in actionable]
        if after_sid in ids:
            return actionable[(ids.index(after_sid) + 1) % len(actionable)]
    return actionable[0]


def _next_actionable_in_table_order(
    flat_rows: list["Session"], current_index: int | None
) -> "Session | None":
    """Cursor-order counterpart to find_next_actionable(), for the "n" key
    specifically. find_next_actionable() cycles by urgency (needs_approval
    first, then oldest-waiting), which is right when there's no visible
    cursor to reason about (Ctrl+Shift+N), but wrong for a key whose whole
    point is moving your eye down a list you're looking at: walking in
    priority order instead of table order could leap over a row you can
    see is READY to reach an older one further down, which reads as the
    key skipping a row (Max, 2026-08-17: "naked n just skipped a READY
    claude row"). This walks flat_rows (the table's own current order,
    whatever sort/grouping is active) starting just after current_index,
    wrapping around, and returns the first actionable row it reaches, so
    "n" never passes over a row you can see needs you."""
    n = len(flat_rows)
    if n == 0:
        return None
    start = (current_index + 1) if current_index is not None else 0
    for offset in range(n):
        s = flat_rows[(start + offset) % n]
        if s.status in ACTIONABLE_STATUSES and not s.is_subagent:
            return s
    return None


def _a_monitor_is_running() -> bool:
    """Fast (<10ms) liveness check: is anything holding the click-to-jump
    HTTP port. on_mount() binds it unconditionally outside pytest and
    schedules the request-file poller in the same call, so a bound port is
    a reliable proxy for "a poller is alive to consume what we're about to
    write." Used to skip the request/poll dance entirely when nothing is
    listening, rather than burning a full timeout waiting on a request
    nobody can ever consume."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", JUMP_HTTP_PORT)) == 0


# Outcomes of handing a request to a running monitor. Only UNSERVED means
# "nobody will act on this": CONSUMED means the poller took our token,
# SUPERSEDED means a later request overwrote ours before the poller read it
# (the poller will serve THAT one instead, so a jump still happens). A
# caller must fall back to doing the work itself on UNSERVED only; falling
# back on SUPERSEDED double-jumps.
REQ_CONSUMED = "consumed"
REQ_SUPERSEDED = "superseded"
REQ_UNSERVED = "unserved"


def _drop_request_and_await_consumption(sentinel: str, timeout: float = 1.0,
                                         poll: float = 0.05) -> str:
    """Write `sentinel:<unique token>` into the shared jump-request file
    click-to-jump links, --jump-next, and --restart all use, and wait for
    a live monitor's poller (_check_jump_request, every 200ms) to consume
    it. Shared by --jump-next's fast path and --restart so a future change
    to the timeout/poll/liveness-check logic only has one copy to update.

    Skips straight to failure if _a_monitor_is_running() says nothing is
    listening: before this check, --jump-next with no monitor running
    still burned a full extra second here before falling back to its own
    cold scan, on top of that scan's several seconds, making the no-monitor
    case slower than doing nothing here at all (caught by review,
    2026-08-16).

    Checks the file still holds OUR token before declaring success, not
    just that it's gone: two overlapping requests (mashing the hotkey
    faster than the 200ms poll, or racing an unrelated click-to-jump link)
    would otherwise both see "file gone" and both report success even
    though only whichever one the poller actually read produced a real
    jump (also caught by review). Returns one of REQ_CONSUMED,
    REQ_SUPERSEDED, REQ_UNSERVED; see their comment for what a caller may
    do with each."""
    if not _a_monitor_is_running():
        return REQ_UNSERVED
    token = f"{sentinel}:{os.getpid()}:{time.monotonic_ns()}"
    try:
        JUMP_REQUEST_PATH.write_text(token)
    except OSError:
        return REQ_UNSERVED
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = JUMP_REQUEST_PATH.read_text()
        except OSError:
            return REQ_CONSUMED  # gone: consumed, and it was still ours a moment ago
        if current != token:
            return REQ_SUPERSEDED  # overwritten before being read
        time.sleep(poll)
    # Deadline. Re-read BEFORE unlinking: the monitor's main thread can
    # legitimately stall past our timeout (a menu jump runs
    # focus_terminal_session synchronously on it; Shift+R's git pull can
    # block up to 15s), so the poller may consume our token in the last
    # instant. A blind unlink here then sent the caller down the cold
    # fallback path: two jumps, next_cursor advanced twice, and a second
    # process writing prefs under the running monitor (advisor review,
    # 2026-08-18). Gone or changed means consumed/superseded, so report
    # accordingly and never fall back; only a file still holding OUR token
    # is a genuinely unserved request worth cleaning up and retrying cold.
    try:
        current = JUMP_REQUEST_PATH.read_text()
    except OSError:
        return REQ_CONSUMED
    if current != token:
        return REQ_SUPERSEDED
    JUMP_REQUEST_PATH.unlink(missing_ok=True)
    return REQ_UNSERVED


def _try_fast_jump_next() -> str:
    """Ask an already-running monitor to jump, via the shared request-file
    protocol. Near-instant because that monitor's session list is already
    warm in memory. The alternative, jump_to_next_actionable() below, calls
    parse_sessions() cold in a brand-new process, which took ~6.5s against
    this machine's real session count (measured 2026-08-16), the actual
    cause of Ctrl+Shift+N feeling slow. Returns the REQ_* outcome; the
    caller may run the cold path on REQ_UNSERVED only."""
    return _drop_request_and_await_consumption(JUMP_NEXT_SENTINEL)


def _focus_or_resume_target(target: "Session") -> tuple[bool, str]:
    """Shared by every jump path that lands on one specific target session:
    focus its window, resume it in a new one if the window can't be found
    and the process isn't alive, and mark it seen (READY read/unread) only
    once one of those two actually lands, never on a bare attempt. Pulled
    out 2026-08-16 after the mark-before-verify bug (caught by review) had
    to be fixed at two near-identical copies of this exact sequence; a
    third was about to become a fourth."""
    if focus_terminal_session(target):
        _mark_ready_seen(target.session_id, target.status, target.last_activity)
        return True, target.title
    if _is_session_alive(target.session_id):
        mlog("DIVERGE", "alive_but_unfound", sid=target.session_id[:12], title=target.title,
             candidates=_resolve_match_candidates(target))
        _heal_hook_state(target.session_id)
        return False, f"Window not found for {target.title[:20]}. Press Enter → Resume."
    if resume_session(target):
        _mark_ready_seen(target.session_id, target.status, target.last_activity)
        return True, f"Resuming {target.title[:20]} in new window"
    return False, f"Could not find or resume {target.title[:20]}"


def jump_to_next_actionable() -> tuple[bool, str, "Session | None"]:
    """Find and jump to the next session that needs you. Persists which
    session it landed on (in monitor-prefs.json) so the NEXT call, whether
    that's pressing n again inside the app or Ctrl+Shift+N from anywhere
    else, continues the same walk through the queue rather than repeating.
    Runs the same dedupe_scheduled_sessions() pass the TUI's own refresh
    does: without it this candidate set can quietly disagree with the
    in-app "n" key's (self.sessions is already deduped), so the two
    "shared cursor" entry points could advance next_cursor to different
    targets."""
    pinned = load_pinned_sessions()
    sessions = parse_sessions(include_archived=False, include_subagents=False, pinned=pinned)
    sessions = dedupe_scheduled_sessions(sessions, pinned)
    prefs = load_prefs()
    acked_ready = _normalize_acked_ready(prefs.get("acked_ready", {}))
    target = find_next_actionable(sessions, prefs.get("next_cursor"), acked_ready)
    if target is None:
        return False, "Nothing needs you right now", None

    _update_prefs(lambda p: p.__setitem__("next_cursor", target.session_id) or True)
    ok, message = _focus_or_resume_target(target)
    return ok, message, target


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--log":
        from monitor_log import tail_log
        cat_filter = sys.argv[2] if len(sys.argv) > 2 else None
        tail_log(category=cat_filter)
    elif len(sys.argv) > 1 and sys.argv[1] == "--reconcile":
        sys.exit(min(run_reconcile_report(), 1))
    elif len(sys.argv) > 1 and sys.argv[1] == "--restart":
        # Tells an already-running monitor to restart in place (same
        # window, same PID, os.execv) and pick up code changes, the same
        # thing Shift+R does, without a synthetic keystroke into that
        # window (which fires Claude Nest's push-to-talk regardless of
        # which key is sent). No-op if nothing is running to pick it up.
        outcome = _drop_request_and_await_consumption(RESTART_SENTINEL)
        sys.exit(0 if outcome != REQ_UNSERVED else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "--save-layout":
        result = save_layout()
        if not result.get("ok"):
            print(f"Not saved: {result.get('reason', 'unknown')}. "
                  f"Previous snapshot at {LAYOUT_PATH} left untouched.")
            sys.exit(1)
        r = result["summary"]
        prev = f" (previous snapshot had {r['previous_windows']})" if r.get("previous_windows") is not None else ""
        print(f"Saved {r['windows']} window(s){prev}, {r['claudes']} Claude(s) "
              f"({r['newly_pinned']} newly pinned) to {LAYOUT_PATH}")
        if r["unresolved_tabs"]:
            print(f"  {r['unresolved_tabs']} tab(s) had no Claude marker; kept as plain shells")
        sys.exit(0)
    elif len(sys.argv) > 1 and sys.argv[1] == "--restore-layout":
        r = restore_layout(restore_pins="--restore-pins" in sys.argv,
                           compact="--exact" not in sys.argv,
                           dry_run="--dry-run" in sys.argv)
        if r.get("dry_run"):
            print(f"Would open {r['windows_planned']} window(s), "
                  f"{r['tabs_planned']} tab(s):")
            for w in r["plan"]:
                where = "placed" if w["frame"][2] else "auto"
                head = f"[{w['group']}]" if w["group"] else "[kept]"
                print(f"  {head:14} {where:6} {', '.join(w['tabs'])}")
            if r["skipped_live"]:
                print(f"  ({len(r['skipped_live'])} already running, skipped)")
            sys.exit(0)
        if not r["ok"] and "reason" in r:
            print(r["reason"])
            sys.exit(1)
        print(f"Rebuilt {r['windows_built']}/{r['windows_planned']} window(s)")
        if r.get("windows_unframed"):
            print(f"  {r['windows_unframed']} window(s) opened but could not be positioned")
        if r.get("skipped_live"):
            print(f"  {len(r['skipped_live'])} session(s) already running; not duplicated:")
            for sid in r["skipped_live"]:
                print(f"    {sid}")
        if r["missing"]:
            print(f"  {len(r['missing'])} session(s) no longer resumable (transcript gone):")
            for sid in r["missing"]:
                print(f"    {sid}")
        sys.exit(0 if r["ok"] else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "--jump-next":
        # Ephemeral: no TUI, no visible window of its own. Fast path first:
        # hand off to an already-running monitor's warm session list
        # (near-instant); only cold-scan this process's own parse_sessions()
        # if nothing picked that up, which means no monitor is running.
        outcome = _try_fast_jump_next()
        if outcome != REQ_UNSERVED:
            mlog("jump", "cli_next", ok=True, message=f"handed off to running monitor ({outcome})")
            sys.exit(0)
        ok, message, _target = jump_to_next_actionable()
        mlog("jump", "cli_next", ok=ok, message=message)
        sys.exit(0 if ok else 1)
    else:
        app = ClaudeMonitor()
        app.run()
        if app.return_code == RESTART_EXIT_CODE:
            os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
