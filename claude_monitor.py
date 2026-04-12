#!/usr/bin/env python3
"""Claude Code session monitor — btop-style TUI."""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

def _escape_markup(text: str) -> str:
    """Escape all [ for Textual markup (rich.markup.escape misses some)."""
    return text.replace("[", "\\[")
from textual.app import App, ComposeResult
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
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PREFS_PATH = Path.home() / ".claude" / "monitor-prefs.json"
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
    "duration":  {"label": "Duration", "default": True},
    "doing":     {"label": "Doing",    "default": True},
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
    "working": 0, "debriefing": 1, "needs_approval": 2,
    "waiting": 3, "idle": 4, "closed": 5, "archived": 6,
}
STATUS_DISPLAY = {
    "working": ("● WORKING", "green"),
    "debriefing": ("⏳ DEBRIEFING", "magenta"),
    "needs_approval": ("◉ APPROVE", "yellow"),
    "waiting": ("○ WAITING", "dark_orange"),
    "idle": ("◌ IDLE", "dim"),
    "closed": ("⊘ CLOSED", "rgb(100,100,100)"),
    "archived": ("◇ ARCHIVED", "dim"),
}


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
    parent_id: str = ""
    subagents: list["Session"] = field(default_factory=list)
    compact_count: int = 0
    mcp_calls: int = 0
    last_tool: str = ""
    last_tool_input: dict = field(default_factory=dict)
    last_assistant_text: str = ""
    status_name: str = ""
    project_path: str = ""  # Original launch directory (for resume)


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


def scan_full_file(path: str, stale_ok: bool = False) -> dict:
    """Single-pass full file scan: tokens, MCP, title, slug, created, last activity.

    Results are cached by (path, mtime) — unchanged files return instantly.
    When stale_ok=True, returns the last cached result even if mtime changed
    (hook state provides fresher status/tool data; tokens/model are slow-changing).
    """
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
    }

    try:
        with open(path, "r") as f:
            for line in f:
                if not line.strip():
                    continue

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
                   include_subagents: bool = False) -> list[Session]:
    t0 = time.perf_counter()
    sessions = []
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

        is_archived = mtime < active_cutoff
        if is_archived and not include_archived:
            if not _is_session_alive(session_id):
                tg = time.perf_counter()
                continue
            is_archived = False
        if mtime < archive_cutoff and not _is_session_alive(session_id):
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
                if session.tokens_out <= 20:
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
        # Build title from best available source
        hook = read_hook_state(sid)
        sl_name = _read_session_cache("name", sid)
        title = (
            pdata.get("name")
            or sl_name
            or (hook.get("title") if hook else "")
            or "Claude"
        )
        status = "idle"
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

    if _PERF:
        print(f"[perf]   parse_sessions: rglob iter: {t_rglob*1000:.1f}ms", file=sys.stderr)
        print(f"[perf]   parse_sessions: build_session: {t_build*1000:.1f}ms "
              f"({n_scanned} sessions, {n_full_scan} full scans, {n_stale_ok} cached/stale-ok)", file=sys.stderr)
        print(f"[perf]   parse_sessions: count_compactions: {t_compact*1000:.1f}ms", file=sys.stderr)
        print(f"[perf]   parse_sessions: subagents: {t_subagent*1000:.1f}ms", file=sys.stderr)
        print(f"[perf]   parse_sessions: _is_session_alive checks: {n_alive_check}", file=sys.stderr)
        if n_orphans:
            print(f"[perf]   parse_sessions: PID-file orphans added: {n_orphans}", file=sys.stderr)
    return sessions


def _read_session_cache(kind: str, session_id: str) -> str:
    """Read /tmp/claude-{kind}-{session_id}, return stripped string or empty."""
    try:
        return Path(f"/tmp/claude-{kind}-{session_id}").read_text().strip()
    except OSError:
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
        display_title = (
            data["custom_title"]
            or hook_title
            or idx.get("summary", "")
            or read_session_memory_title(path)
            or idx.get("firstPrompt", "")[:60]
            or Path(data["cwd"]).name
            or session_id[:8]
        )

    status = determine_status(session_id, data["last_assistant_time"], display_title)

    # Context %: how much context is USED (burnt).
    # Statusline cache stores remaining %, so we flip it.
    try:
        remaining = int(_read_session_cache("ctx", session_id))
        context_pct = max(0, min(100, 100 - remaining))
    except ValueError:
        last_input = data["last_input_tokens"]
        if last_input == 0:
            context_pct = 0  # Nothing used yet
        else:
            context_pct = min(100, int((last_input / 200000) * 100))

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

    return Session(
        session_id=session_id, project=project,
        title=display_title[:50], status=status,
        model=format_model(data["model_id"]), model_id=data["model_id"],
        cost=cost, tokens_in=data["tokens_in"], tokens_out=data["tokens_out"],
        context_pct=context_pct,
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
            os.kill(int(hook["pid"]), 0)
            return True
        except (OSError, ValueError):
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


def _reconcile_sessions() -> None:
    """Periodic sweep: heal stale hook states, refresh names from /tmp, and
    re-stamp terminal titles with ·sid8 markers. PID files are the authority
    for what's running — everything else is healed to match."""
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
    mlog("reconcile", "sweep", healed=healed, stamped=stamped)


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


def determine_status(session_id: str, last_assistant_time: float,
                     display_title: str = "") -> str:
    alive = _is_session_alive(session_id)
    if not alive:
        return "closed"

    # Tier 1: hook state files (real-time, event-driven)
    hook = read_hook_state(session_id)
    if hook:
        hook_state = hook.get("state", "")
        if hook_state == "thinking":
            return "working"
        if hook_state == "approval":
            return "needs_approval"
        if hook_state == "exited":
            return "closed"
        if hook_state == "idle":
            # Decay waiting → idle after 5 minutes of inactivity
            entered = hook.get("state_entered_at", "")
            if entered:
                try:
                    elapsed = time.time() - parse_timestamp(entered)
                    return "idle" if elapsed > 300 else "waiting"
                except (ValueError, TypeError):
                    pass
            return "waiting"

    # Tier 2: signal files (legacy)
    if SIGNALS_DIR.exists():
        signal_file = SIGNALS_DIR / session_id
        if signal_file.exists():
            try:
                s = signal_file.read_text().strip()
                return {"working": "working", "permission": "needs_approval",
                        "stop": "waiting"}.get(s, "idle")
            except OSError:
                pass

    # Tier 3: time-based heuristics (fallback)
    now = time.time()
    if last_assistant_time > 0:
        elapsed = now - last_assistant_time
        if elapsed < 30:
            return "working"
        elif elapsed < 300:
            return "waiting"
    return "idle"


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


def format_context_bar(pct: int, width: int = 10) -> str:
    """Render context usage bar. pct = % of context USED (higher = worse)."""
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
    Approval → prompt:    "Awaiting approval — Editing config"
    Waiting → gerund:     "Editing claude_monitor.py"
    Idle → past tense:    "Edited claude_monitor.py"
    """
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
    elif s.status in ("idle", "waiting", "closed"):
        return _to_past_tense(gerund)
    else:
        return gerund


def sort_sessions(sessions: list[Session], mode: SortMode) -> list[Session]:
    if mode == SortMode.ACTIVITY:
        return sorted(sessions, key=lambda s: s.last_activity, reverse=True)
    elif mode == SortMode.STATUS:
        return sorted(sessions, key=lambda s: (STATUS_PRIORITY.get(s.status, 9), -s.last_activity))
    elif mode == SortMode.ALPHA:
        return sorted(sessions, key=lambda s: (s.title or s.session_id).lower())
    elif mode == SortMode.CONTEXT:
        return sorted(sessions, key=lambda s: s.context_pct)
    elif mode == SortMode.TOKENS:
        return sorted(sessions, key=lambda s: s.tokens_out, reverse=True)
    elif mode == SortMode.COST:
        return sorted(sessions, key=lambda s: s.cost, reverse=True)
    return sessions


# ── Column rendering ──────────────────────────────────────────────────────────


def render_status_cell(status: str, spin_idx: int = 0) -> str:
    icon, color = STATUS_DISPLAY.get(status, ("?", "white"))
    if status == "working":
        frame = SPINNER_FRAMES[spin_idx % len(SPINNER_FRAMES)]
        return f"[#D97757]{frame}[/] [{color}]WORKING[/]"
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


def render_row(s: Session, visible_cols: list[str], spin_idx: int = 0) -> list[str]:
    cells = []
    for col in visible_cols:
        if col == "status":
            cells.append(render_status_cell(s.status, spin_idx))
        elif col == "session":
            if s.is_subagent:
                cells.append(f"[dim]└─ {s.title}[/]")
            else:
                t = s.title
                if s.subagents:
                    t += f" [dim](+{len(s.subagents)})[/]"
                cells.append(t)
        elif col == "project":
            cells.append(s.project if not s.is_subagent else "")
        elif col == "model":
            cells.append(s.model)
        elif col == "context":
            cells.append("" if s.is_subagent else format_context_bar(s.context_pct))
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
                if s.status == "idle":
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
            const sidWindow = allTitles.find(t => t && t.includes(sid8));
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
            for (let i = 1; i < candidates.length; i++) {{
                const m = allTitles.find(t => t && nameMatch(t, candidates[i]));
                if (m) {{ nameWindow = m; nameCand = candidates[i]; break; }}
            }}

            // Phase 2: resolve conflicts between sid marker and name match
            if (sidWindow && nameWindow && sidWindow === nameWindow) {{
                // Both point to the same window — ideal case
                targetName = sidWindow; matchedCand = sid8;
            }} else if (sidWindow && nameWindow && sidWindow !== nameWindow) {{
                // ·sid8 marker is on a DIFFERENT window than the name
                // match — the marker is stale (old tab from a resume,
                // or Claude's auto-summary clobbered the marker on the
                // active tab). Prefer the name match.
                targetName = nameWindow;
                matchedCand = nameCand + "+sid_stale";
            }} else if (sidWindow) {{
                targetName = sidWindow; matchedCand = sid8;
            }} else if (nameWindow) {{
                targetName = nameWindow; matchedCand = nameCand;
            }}
            if (!targetName) continue;

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
                if (thenText) {{ delay(0.15); se.keystroke(thenText); se.keyCode(36); }}
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
                    if (thenText) {{ delay(0.3); se.keystroke(thenText); se.keyCode(36); }}
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


def _auto_rename_after_resume(old_name: str, prior_sids: set[str]) -> None:
    """Wait for the resumed session's window to appear, then send /rename.

    Polls for a new ·sid8 marker (one not in prior_sids) — that's the
    resumed session's hook-written title. Raises that specific window
    before typing, so focus loss during the wait doesn't misfire.
    """
    deadline = time.time() + 15
    new_sid8 = None
    while time.time() < deadline:
        time.sleep(0.8)
        current = _snapshot_window_sids()
        fresh = current - prior_sids
        if fresh:
            new_sid8 = next(iter(fresh))
            break
    if not new_sid8:
        mlog("DIVERGE", "auto_rename_no_new_window",
             name=old_name, prior_sids=sorted(prior_sids))
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
            const target = titles.find(t => t && t.includes(marker));
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
    except (subprocess.TimeoutExpired, OSError):
        mlog("resume", "auto_rename_error", name=old_name, new_sid8=new_sid8)


def resume_session(session: Session) -> bool:
    """Resume a Claude session in a new Ghostty tab (falls back to Terminal.app)."""
    cmd = f"claude --resume {session.session_id}"
    # Claude CLI resolves sessions by hashing the cwd. Use the original
    # launch directory: sessions-index projectPath > transcript path > last cwd.
    cwd = (
        session.project_path
        or _derive_cwd_from_transcript(session.transcript_path)
        or session.cwd
        or str(Path.home())
    )

    # Verify the JSONL transcript exists before trying to resume
    jsonl_exists = bool(session.transcript_path) and Path(session.transcript_path).exists()
    mlog("resume", "attempt", sid=session.session_id[:12], title=session.title,
         cwd=cwd, jsonl_exists=jsonl_exists)
    if not jsonl_exists:
        mlog("resume", "no_jsonl", sid=session.session_id[:12],
             path=session.transcript_path)
        return False

    # Snapshot existing ·sid8 markers so auto-rename can detect the new one
    prior_sids = _snapshot_window_sids()

    # Ghostty: open new tab via keystroke, then type the command
    quoted_cwd = shlex.quote(cwd)
    jxa = f"""(() => {{
        const se = Application("System Events");
        const cwd = {json.dumps(quoted_cwd)};
        const cmd = {json.dumps(cmd)};

        // Try Ghostty, then iTerm2 (both use Cmd+T for new tab)
        for (const appName of ["Ghostty", "iTerm2"]) {{
            try {{
                const proc = se.processes.byName(appName);
                proc.name();
                proc.frontmost = true;
                delay(0.2);
                se.keystroke("t", {{using: "command down"}});
                delay(0.5);
                se.keystroke("cd " + cwd + " && " + cmd);
                delay(0.1);
                se.keyCode(36);
                return appName;
            }} catch(e) {{}}
        }}

        // Fall back to Terminal.app
        const term = Application("Terminal");
        term.activate();
        term.doScript("cd " + cwd + " && " + cmd);
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
        if result.returncode == 0 and out in ("Ghostty", "iTerm2", "Terminal"):
            _recently_resumed[session.session_id] = time.time()
            # Auto-rename the new session to match the old name. Skip junk
            # fallback titles (home-dir basename, sid8, generic placeholders)
            # — applying those would overwrite a better auto-generated name.
            junk = {"", "Claude", "Claude Code", "~",
                    Path.home().name, session.session_id[:8]}
            if session.title and session.title not in junk and len(session.title) >= 3:
                threading.Thread(
                    target=_auto_rename_after_resume,
                    args=(session.title, prior_sids),
                    daemon=True,
                ).start()
            return True
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        mlog("resume", "launch_error", sid=session.session_id[:12], error=str(e))
        return False


# ── Screens ───────────────────────────────────────────────────────────────────

# Kanban columns: status key → display header
KANBAN_COLUMNS = [
    ("closed",         "⊘ Closed",   "grey50"),
    ("idle",           "◌ Idle",     "grey70"),
    ("waiting",        "○ Waiting",  "dark_orange"),
    ("needs_approval", "◉ Approval", "yellow"),
    ("working",        "● Working",  "green"),
]


_SPIN_BASE = "·*✢✳✶✻"
SPINNER_FRAMES = _SPIN_BASE + _SPIN_BASE[-2:0:-1]  # ping-pong: up then back down
STATUS_ICON = {
    "working": None,  # animated — uses SPINNER_FRAMES
    "needs_approval": "?",
    "waiting": ".",
    "idle": "◌",
    "closed": "x",
}

STATUS_COLOR = {
    "working": "#5B8A72",
    "needs_approval": "#B0A04F",
    "waiting": "#B07D4F",
    "idle": "#606060",
    "closed": "#404040",
    "archived": "#404040",
    "debriefing": "#8B668B",
}

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


class KanbanView(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("v", "next_view", "View"),
        Binding("up", "move('up')", "↑", show=False),
        Binding("down", "move('down')", "↓", show=False),
        Binding("left", "move('left')", "←", show=False),
        Binding("right", "move('right')", "→", show=False),
        Binding("enter", "select", "Select"),
        # Mirrored from main view
        Binding("r", "passthrough('refresh')", "Refresh"),
        Binding("h", "passthrough('toggle_archived')", "History", show=False),
        Binding("d", "passthrough('toggle_debug')", "Debug", show=False),
        Binding("t", "passthrough('toggle_theme')", "Theme", show=False),
        Binding("R", "passthrough('restart')", "Restart", show=False),
        Binding("n", "edit_name", "Name", show=False),
        Binding("g", "toggle_groups", "Group", show=False),
    ]
    CSS = """
    KanbanView { background: $background; align: center middle; }
    #kanban-outer {
        width: 100%; height: 100%; padding: 0 1;
    }
    #kanban-title { text-align: center; text-style: bold; height: 1; }
    #kanban-board {
        width: 100%; height: 1fr;
    }
    .kanban-col {
        width: 1fr; height: 100%; padding: 0;
        border-right: solid $panel;
    }
    .kanban-col:last-child { border-right: none; }
    .kanban-col-header {
        text-align: center; text-style: bold; height: 1;
    }
    .kanban-cards {
        height: 1fr; overflow-y: auto;
    }
    .kanban-card {
        height: auto; padding: 0; margin: 0;
        background: $panel; border: solid $primary-darken-2;
    }
    .kanban-card.-selected {
        border: thick $accent;
        background: $boost;
    }
    .kanban-empty { color: $text-disabled; text-align: center; }
    """

    def __init__(self, sessions: list[Session], by_group: bool = False) -> None:
        super().__init__()
        self._spin_idx = 0
        self._col = 0
        self._row = 0
        self._grid: list[list[tuple[Session, str]]] = []
        self._working_col = -1
        self._by_group = by_group
        self._columns: list[tuple[str, str, str]] = []
        self._populate_grid(sessions)

    def _make_body(self, s: Session, group_key: str = "") -> str:
        name = s.title
        if group_key and self._by_group and name != group_key:
            prefix = group_key
            for sep in ("-", "_", "/", " ", "."):
                if name.startswith(prefix + sep):
                    name = name[len(prefix):]
                    break
        # Break on hyphens for wrapping, but preserve leading hyphen on line 1
        if name.startswith("-"):
            title = _escape_markup("-" + name[1:].replace("-", "-\n"))
        else:
            title = _escape_markup(name.replace("-", "-\n"))
        activity = generate_activity(s)
        body = f"{title}[/]"
        if activity:
            body += f"\n[dim]{_escape_markup(activity[:36])}[/]"
        return body

    def _populate_grid(self, sessions: list[Session]) -> None:
        """Build grid[col] = [(session, body_text), ...] — body precomputed."""
        active = [s for s in sessions if not s.is_subagent]

        if self._by_group:
            groups: dict[str, list[Session]] = {}
            for s in active:
                groups.setdefault(_group_key(s.title), []).append(s)
            singles = [k for k in list(groups) if len(groups[k]) < 2]
            if singles:
                ung = groups.setdefault("ungrouped", [])
                for k in singles:
                    if k != "ungrouped":
                        ung.extend(groups.pop(k))
            keys = sorted(groups, key=lambda k: (k == "ungrouped", k.lower()))
            self._columns = [(k, k, "cyan") for k in keys]
            self._grid = [[(s, self._make_body(s, group_key=k)) for s in groups[k]] for k in keys]
            self._working_col = -1
        else:
            self._columns = list(KANBAN_COLUMNS)
            valid = {k for k, _, _ in KANBAN_COLUMNS}
            col_idx = {k: i for i, (k, _, _) in enumerate(KANBAN_COLUMNS)}
            self._grid = [[] for _ in KANBAN_COLUMNS]
            for s in active:
                bucket = s.status if s.status in valid else "closed"
                self._grid[col_idx[bucket]].append((s, self._make_body(s)))
            self._working_col = col_idx.get("working", -1)

        self._col = next((i for i, c in enumerate(self._grid) if c), 0)
        self._row = 0

    def _card_text(self, col_idx: int, body: str, session: Session | None = None) -> str:
        key = self._columns[col_idx][0]
        is_working = (key == "working") or (session and session.status == "working")
        if is_working:
            icon = SPINNER_FRAMES[self._spin_idx % len(SPINNER_FRAMES)]
            return f"[bold][#D97757]{icon}[/#D97757] {body}"
        icon = STATUS_ICON.get(key, "·")
        return f"[bold]{icon} {body}"

    def on_mount(self) -> None:
        has_working = any(
            s.status == "working"
            for col in self._grid for s, _ in col
        )
        if has_working:
            self.set_interval(0.132, self._tick_spinner)

    def _tick_spinner(self) -> None:
        self._spin_idx += 1
        for col_idx, col in enumerate(self._grid):
            for row_idx, (s, body) in enumerate(col):
                if s.status == "working":
                    try:
                        card = self.query_one(f"#kc-{col_idx}-{row_idx}", Static)
                        card.update(self._card_text(col_idx, body, session=s))
                    except Exception:
                        pass

    def compose(self) -> ComposeResult:
        mode = "Groups" if self._by_group else "Status"
        with Vertical(id="kanban-outer"):
            yield Label(f"[bold]Kanban · {mode}[/]  [dim]←→↑↓ nav · enter select · esc close[/]", id="kanban-title")
            with Horizontal(id="kanban-board"):
                for col_idx, (key, header, color) in enumerate(self._columns):
                    with Vertical(classes="kanban-col"):
                        count = len(self._grid[col_idx])
                        yield Label(
                            f"[{color}]{header}[/] [dim]({count})[/]",
                            classes="kanban-col-header",
                        )
                        with Vertical(classes="kanban-cards"):
                            if not self._grid[col_idx]:
                                yield Static("[dim]—[/]", classes="kanban-empty")
                            for row_idx, (s, body) in enumerate(self._grid[col_idx]):
                                sel = col_idx == self._col and row_idx == self._row
                                classes = "kanban-card -selected" if sel else "kanban-card"
                                yield Static(self._card_text(col_idx, body, session=s),
                                             classes=classes,
                                             id=f"kc-{col_idx}-{row_idx}")
        yield Footer()

    def _refresh_selection(self) -> None:
        for w in self.query(".kanban-card"):
            w.remove_class("-selected")
        try:
            card = self.query_one(f"#kc-{self._col}-{self._row}")
            card.add_class("-selected")
            card.scroll_visible(animate=False)
        except Exception:
            pass

    def action_move(self, direction: str) -> None:
        if direction in ("left", "right"):
            step = -1 if direction == "left" else 1
            new_col = self._col
            for _ in range(len(self._grid)):
                new_col = (new_col + step) % len(self._grid)
                if self._grid[new_col]:
                    break
            self._col = new_col
            self._row = min(self._row, len(self._grid[self._col]) - 1)
        else:
            col_len = len(self._grid[self._col])
            if col_len:
                step = -1 if direction == "up" else 1
                self._row = (self._row + step) % col_len
        self._refresh_selection()

    def action_select(self) -> None:
        col = self._grid[self._col] if 0 <= self._col < len(self._grid) else []
        if 0 <= self._row < len(col):
            s, _ = col[self._row]
            handler = self.app._make_menu_handler(s)  # type: ignore[attr-defined]
            self.app.push_screen(SessionMenu(s), handler)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_next_view(self) -> None:
        self.dismiss("__next_view__")

    def action_passthrough(self, name: str) -> None:
        """Delegate to the main app's action, then refresh this view."""
        getattr(self.app, f"action_{name}")()
        if name in ("refresh", "toggle_archived"):
            self._rebuild_grid()

    def action_edit_name(self) -> None:
        col = self._grid[self._col] if 0 <= self._col < len(self._grid) else []
        if 0 <= self._row < len(col):
            self.app.action_edit_name()  # type: ignore[attr-defined]

    def action_toggle_groups(self) -> None:
        self._by_group = not self._by_group
        # Sync with main app so closing kanban keeps the setting
        self.app.show_groups = self._by_group  # type: ignore[attr-defined]
        self._rebuild_grid()

    def _rebuild_grid(self) -> None:
        """Re-populate cards from fresh app session data after a passthrough."""
        self._populate_grid(getattr(self.app, "sessions", []))
        self.refresh(recompose=True)


class TimelineView(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("v", "next_view", "View"),
        Binding("up", "move('up')", "↑", show=False),
        Binding("down", "move('down')", "↓", show=False),
        Binding("enter", "select", "Select"),
        Binding("p", "cycle_range", "Period"),
        Binding("r", "passthrough('refresh')", "Refresh"),
        Binding("h", "passthrough('toggle_archived')", "History", show=False),
        Binding("d", "passthrough('toggle_debug')", "Debug", show=False),
        Binding("t", "passthrough('toggle_theme')", "Theme", show=False),
        Binding("R", "passthrough('restart')", "Restart", show=False),
        Binding("n", "edit_name", "Name", show=False),
        Binding("g", "toggle_groups", "Group", show=False),
        Binding("pageup", "prev_group", "PgUp", show=False, priority=True),
        Binding("pagedown", "next_group", "PgDn", show=False, priority=True),
    ]
    CSS = """
    TimelineView { background: $background; }
    #timeline-outer { width: 100%; height: 100%; padding: 0 1; }
    #timeline-title { text-align: center; text-style: bold; height: 1; }
    #timeline-axis { height: 1; overflow: hidden; }
    #timeline-sep { height: 1; color: $text-disabled; overflow: hidden; }
    .timeline-chart { height: 1fr; overflow-y: auto; }
    .timeline-group { height: 1; }
    .timeline-bar { height: 1; }
    .timeline-bar.-selected { background: $boost; }
    .timeline-spacer { height: 1; }
    #timeline-summary {
        height: auto; max-height: 25%; min-height: 3; padding: 0 2;
        background: $boost; dock: bottom; border-top: solid $primary;
        overflow-y: auto;
    }
    """

    def __init__(self, sessions: list[Session], by_group: bool = False) -> None:
        super().__init__()
        self._spin_idx = 0
        self._sel = 0
        self._by_group = by_group
        self._time_range = "week"
        self._flat: list[Session] = []
        self._rows: list[Session | str | None] = []  # Session, group-key str, or None
        self._group_first_sel: list[int] = []  # _sel indices of first session per group
        self._t_min = 0.0
        self._t_max = 0.0
        self._range_secs = 0.0
        self._chart_width = 80
        self._populate(sessions)

    def _populate(self, sessions: list[Session]) -> None:
        active = [s for s in sessions if not s.is_subagent]
        now = time.time()

        # Filter by time range
        lookback = 7 * 86400 if self._time_range == "week" else 30 * 86400
        cutoff = now - lookback
        active = [s for s in active if s.last_activity >= cutoff or s.created >= cutoff]

        if not active:
            self._flat = []
            self._rows = []
            self._group_first_sel = []
            self._t_min = now - lookback
            self._t_max = now
            self._range_secs = lookback
            return

        self._t_min = now - lookback
        self._t_max = now
        self._range_secs = max(self._t_max - self._t_min, 60)

        flat: list[Session] = []
        rows: list[Session | str | None] = []
        group_first_sel: list[int] = []

        if self._by_group:
            groups: dict[str, list[Session]] = {}
            for s in active:
                groups.setdefault(_group_key(s.title), []).append(s)
            singles = [k for k in list(groups) if len(groups[k]) < 2]
            if singles:
                ung = groups.setdefault("ungrouped", [])
                for k in singles:
                    if k != "ungrouped":
                        ung.extend(groups.pop(k))
            keys = sorted(groups, key=lambda k: (k == "ungrouped", k.lower()))
            first_group = True
            for gk in keys:
                if not first_group:
                    rows.append(None)  # spacer between groups
                first_group = False
                rows.append(gk)  # group header
                group_first_sel.append(len(flat))
                for s in groups[gk]:
                    rows.append(s)
                    flat.append(s)
        else:
            for s in active:
                rows.append(s)
                flat.append(s)

        self._flat = flat
        self._rows = rows
        self._group_first_sel = group_first_sel
        if flat:
            self._sel = min(self._sel, len(flat) - 1)

    def _render_bar(self, s: Session) -> str:
        cw = self._chart_width
        if cw <= 0:
            return ""
        now = time.time()
        start = time_to_col(s.created, self._t_min, self._range_secs, cw)
        end_ts = now if s.status in ("working", "waiting", "needs_approval") else s.last_activity
        end = time_to_col(end_ts, self._t_min, self._range_secs, cw)
        end = max(end, start + 1)
        end = min(end, cw)

        color = STATUS_COLOR.get(s.status, "#404040")
        chars = ["·"] * cw
        marks: dict[int, tuple[str, str]] = {}

        for i in range(start, end):
            chars[i] = "▬"

        # Start marker
        marks[start] = ("▸", "dim")

        # Compaction events — space evenly along the bar
        if s.compact_count > 0:
            bar_len = end - start
            for ci in range(s.compact_count):
                pos = start + int((ci + 1) * bar_len / (s.compact_count + 1))
                if start < pos < end - 1:
                    marks[pos] = ("◆", "dim")

        # Working spinner at trailing edge
        if s.status == "working":
            frame = SPINNER_FRAMES[self._spin_idx % len(SPINNER_FRAMES)]
            marks[min(end - 1, cw - 1)] = (frame, "#D97757")

        bar_str = ""
        for i, c in enumerate(chars):
            if i in marks:
                mc, mc_color = marks[i]
                bar_str += f"[{mc_color}]{mc}[/{mc_color}]"
            elif start <= i < end:
                bar_str += f"[{color}]{c}[/]"
            else:
                bar_str += f"[#2a2a2a]{c}[/]"

        return bar_str

    def _render_row(self, row_entry: Session | str) -> str:
        if isinstance(row_entry, str):
            # Group header
            count = sum(1 for r in self._rows if isinstance(r, Session) and _group_key(r.title) == row_entry)
            style = "dim" if row_entry == "ungrouped" else "bold cyan"
            return f"[{style}]▸ {row_entry}[/] [dim]({count})[/]"
        s = row_entry
        display_name = s.title
        if self._by_group:
            gk = _group_key(s.title)
            if display_name != gk and display_name.startswith(gk):
                display_name = display_name[len(gk):]
        name = _escape_markup(display_name[:LABEL_WIDTH - 2])
        padded = name.ljust(LABEL_WIDTH - 2)
        bar = self._render_bar(s)
        return f"  {padded}{bar}"

    def compose(self) -> ComposeResult:
        try:
            self._chart_width = self.app.size.width - LABEL_WIDTH - 2
        except Exception:
            self._chart_width = 80
        self._chart_width = max(self._chart_width, 20)

        group_label = "Groups" if self._by_group else "All"
        range_label = self._time_range.capitalize()
        ticks = generate_ticks(self._t_min, self._t_max, self._chart_width)

        # Build axis line
        axis_chars = [" "] * self._chart_width
        for col, label in ticks:
            for i, ch in enumerate(label):
                pos = col + i
                if 0 <= pos < self._chart_width:
                    axis_chars[pos] = ch
        axis_line = " " * LABEL_WIDTH + "".join(axis_chars)

        # Build separator line with tick marks
        sep_chars = ["─"] * self._chart_width
        for col, _ in ticks:
            if 0 <= col < self._chart_width:
                sep_chars[col] = "┼"
        sep_line = " " * LABEL_WIDTH + "".join(sep_chars)

        with Vertical(id="timeline-outer"):
            yield Label(
                f"[bold]Timeline · {group_label} · {range_label}[/]  [dim]↑↓ nav · enter select · p period · v next view · esc close[/]",
                id="timeline-title",
            )
            yield Static(f"[dim]{axis_line}[/]", id="timeline-axis")
            yield Static(f"[dim]{sep_line}[/]", id="timeline-sep")

            with Vertical(classes="timeline-chart"):
                if not self._rows:
                    yield Static("[dim]No sessions[/]")
                else:
                    widget_idx = 0
                    session_idx = 0
                    for entry in self._rows:
                        if entry is None:
                            yield Static("", classes="timeline-spacer",
                                         id=f"tl-sp-{widget_idx}")
                        elif isinstance(entry, str):
                            yield Static(
                                self._render_row(entry),
                                classes="timeline-group",
                                id=f"tl-g-{widget_idx}",
                            )
                        else:
                            sel = session_idx == self._sel
                            classes = "timeline-bar -selected" if sel else "timeline-bar"
                            yield Static(
                                self._render_row(entry),
                                classes=classes,
                                id=f"tl-{widget_idx}",
                            )
                            session_idx += 1
                        widget_idx += 1
        yield Static(
            "[dim]Select a session and press Enter → Summarize to generate a period summary[/]",
            id="timeline-summary",
        )
        yield Footer()

    def on_mount(self) -> None:
        has_working = any(s.status == "working" for s in self._flat)
        if has_working:
            self.set_interval(0.132, self._tick_spinner)

    def _tick_spinner(self) -> None:
        self._spin_idx += 1
        widget_idx = 0
        for entry in self._rows:
            if entry is None or isinstance(entry, str):
                widget_idx += 1
                continue
            if entry.status == "working":
                try:
                    bar = self.query_one(f"#tl-{widget_idx}", Static)
                    bar.update(self._render_row(entry))
                except Exception:
                    pass
            widget_idx += 1

    def _refresh_selection(self) -> None:
        for w in self.query(".timeline-bar"):
            w.remove_class("-selected")
        widget_idx = 0
        session_idx = 0
        for entry in self._rows:
            if entry is None or isinstance(entry, str):
                widget_idx += 1
                continue
            if session_idx == self._sel:
                try:
                    bar = self.query_one(f"#tl-{widget_idx}", Static)
                    bar.add_class("-selected")
                    bar.scroll_visible(animate=False)
                except Exception:
                    pass
                break
            widget_idx += 1
            session_idx += 1

    def action_move(self, direction: str) -> None:
        if not self._flat:
            return
        step = -1 if direction == "up" else 1
        self._sel = (self._sel + step) % len(self._flat)
        self._refresh_selection()

    def action_select(self) -> None:
        if 0 <= self._sel < len(self._flat):
            s = self._flat[self._sel]

            def _timeline_handler(action: str | None) -> None:
                if action == "summarize":
                    self._summarize_session(s)
                else:
                    handler = self.app._make_menu_handler(s)  # type: ignore[attr-defined]
                    handler(action)

            self.app.push_screen(SessionMenu(s, context="timeline"), _timeline_handler)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_next_view(self) -> None:
        self.dismiss("__next_view__")

    def action_passthrough(self, name: str) -> None:
        getattr(self.app, f"action_{name}")()
        if name in ("refresh", "toggle_archived"):
            self._rebuild()

    def action_edit_name(self) -> None:
        if 0 <= self._sel < len(self._flat):
            self.app.action_edit_name()  # type: ignore[attr-defined]

    def action_toggle_groups(self) -> None:
        self._by_group = not self._by_group
        self.app.show_groups = self._by_group  # type: ignore[attr-defined]
        self._rebuild()

    def action_cycle_range(self) -> None:
        self._time_range = "month" if self._time_range == "week" else "week"
        self._rebuild()

    def action_prev_group(self) -> None:
        if not self._by_group or not self._group_first_sel:
            return
        prev = [i for i in self._group_first_sel if i < self._sel]
        self._sel = prev[-1] if prev else self._group_first_sel[-1]
        self._refresh_selection()

    def action_next_group(self) -> None:
        if not self._by_group or not self._group_first_sel:
            return
        nxt = [i for i in self._group_first_sel if i > self._sel]
        self._sel = nxt[0] if nxt else self._group_first_sel[0]
        self._refresh_selection()

    def _rebuild(self) -> None:
        self._populate(getattr(self.app, "sessions", []))
        self.refresh(recompose=True)

    def _summarize_session(self, session: Session) -> None:
        """Generate a Haiku summary in the background and update the panel."""
        try:
            panel = self.query_one("#timeline-summary", Static)
        except Exception:
            return
        panel.update(f"[dim]Summarizing {_escape_markup(session.title[:30])}...[/]")

        def _worker():
            summary = _generate_session_summary(session, self._time_range)
            try:
                self.call_from_thread(panel.update, summary)
            except Exception:
                pass

        import threading
        threading.Thread(target=_worker, daemon=True).start()


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
        if s.status in ("archived", "closed"):
            options.append(Option("▶   Resume session", id="resume"))
        else:
            options.append(Option("🖥   Jump to terminal", id="jump"))
            options.append(Option("▶   Resume in new tab", id="resume"))
            options.append(Option("🏷   Rename…", id="edit_name"))
        if self.menu_context == "timeline":
            options.append(Option("📊  Summarize period", id="summarize"))
        options.append(Option(f"📋  Copy session ID ({s.session_id[:8]}…)", id="copy_id"))
        if s.remote_url:
            options.append(Option("🔗  Open remote control", id="remote"))
        options.append(Option("📂  Open transcript", id="transcript"))
        if s.status not in ("archived", "closed"):
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


class DosFooter(Static):
    """DOS-style hotkey bar: the key letter is highlighted within the word."""

    DEFAULT_CSS = """
    DosFooter { dock: bottom; height: 1; background: $panel; padding: 0 1; }
    #update-banner { height: 1; background: $boost; color: $warning; display: none; padding: 0 2; }
    """

    def __init__(self, items: list[tuple[str, str]], **kwargs) -> None:
        # items: [(key, label), ...] where key is the highlighted letter
        parts = []
        for key, label in items:
            idx = label.lower().find(key.lower())
            if idx >= 0:
                before = label[:idx]
                letter = label[idx]
                after = label[idx + 1:]
                parts.append(f"{before}[bold #D97757]{letter}[/]{after}")
            else:
                parts.append(f"[bold #D97757]{key}[/] {label}")
        super().__init__("  ".join(parts), **kwargs)


class StatsBar(Horizontal):
    def compose(self) -> ComposeResult:
        yield Label("", id="stats-working")
        yield Label("", id="stats-waiting")
        yield Label("", id="stats-idle")
        yield Label("", id="stats-closed")
        yield Label("", id="stats-total-cost")
        yield Label("", id="stats-sort")
        yield Input(placeholder="🔍 filter...", id="search-bar")
        yield Label("[dim]/ search[/]", id="search-hint")

    def update_stats(self, sessions: list[Session], sort_mode: SortMode) -> None:
        working = sum(1 for s in sessions if s.status == "working")
        waiting = sum(1 for s in sessions if s.status in ("waiting", "needs_approval"))
        idle = sum(1 for s in sessions if s.status == "idle")
        closed = sum(1 for s in sessions if s.status == "closed")
        total_cost = sum(s.cost for s in sessions)

        self.query_one("#stats-working", Label).update(f" [green]● {working} working[/]  ")
        self.query_one("#stats-waiting", Label).update(f" [dark_orange]○ {waiting} waiting[/]  ")
        self.query_one("#stats-idle", Label).update(f" [dim]◌ {idle} idle[/]  ")
        self.query_one("#stats-closed", Label).update(f" [rgb(100,100,100)]⊘ {closed} closed[/]  " if closed else "")
        self.query_one("#stats-total-cost", Label).update(f" [cyan]Σ ${total_cost:.2f}[/]  ")
        self.query_one("#stats-sort", Label).update(f" [magenta]sort: {sort_mode.label}[/]")


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
    #detail-panel {
        height: auto; max-height: 35%; min-height: 5; padding: 0 2;
        background: $boost; dock: bottom; border-top: solid $primary;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("a", "toggle_subagents", "Agents"),
        Binding("h", "toggle_archived", "History"),
        Binding("c", "pick_columns", "Columns"),
        # Binding("l", "statusline_config", "Statusline"),  # TODO: re-enable after statusline merge
        Binding("d", "toggle_debug", "Debug", show=False),
        Binding("K", "setup_api_key", "API Key", show=False),
        Binding("slash", "start_search", "Search", show=False),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("v", "cycle_view", "View"),
        Binding("t", "toggle_theme", "Theme", show=False),
        Binding("R", "restart", "Restart", show=False),
        Binding("j", "cursor_down", "↓", show=False),
        Binding("n", "edit_name", "Name"),
        Binding("P", "proactive_group", "/proactive→group", show=False),
        Binding("g", "toggle_groups", "Group"),
        Binding("pageup", "prev_group", "PgUp", show=False, priority=True),
        Binding("pagedown", "next_group", "PgDn", show=False, priority=True),
        Binding("home", "table_home", "Home", show=False, priority=True),
        Binding("end", "table_end", "End", show=False, priority=True),
    ]

    sort_mode: reactive[SortMode] = reactive(SortMode.ALPHA)
    show_subagents: reactive[bool] = reactive(False)
    show_archived: reactive[bool] = reactive(False)
    show_groups: reactive[bool] = reactive(True)
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
    _dismissing_sessions: dict[str, str] = {}  # sid -> "debriefing" | "closing"
    _dismiss_failed: set[str] = set()  # sids where dismiss failed (can't reach terminal)
    _prev_statuses: dict[str, str] = {}  # sid -> previous status (for transition logging)
    _spin_idx: int = 0
    _last_cursor_row: int = 0

    def notify(self, message, *, timeout: float | None = 5, **kwargs):
        """Override to log every toast notification."""
        mlog("toast", "notify", message=str(message))
        super().notify(message, timeout=timeout, **kwargs)

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatsBar()
        yield Static("", id="update-banner")
        yield DataTable(id="session-table", cursor_type="row")
        yield Static(
            "",
            id="detail-panel"
        )
        yield DosFooter([
            ("Q", "Quit"), ("R", "Refresh"), ("S", "Sort"), ("A", "Agents"),
            ("H", "History"), ("C", "Columns"), ("V", "View"), ("N", "Name"),
            ("G", "Group"),
        ])

    def on_mount(self) -> None:
        t0 = time.perf_counter()
        self.register_theme(GRUVBOX_DARK)
        self.register_theme(GRUVBOX_LIGHT)
        self._visible_cols = get_visible_columns()
        self._col_order = get_column_order()
        saved_theme = load_prefs().get("theme")
        if saved_theme in ("gruvbox-dark", "gruvbox-light"):
            self.theme = saved_theme
        else:
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
        self.set_interval(30, self._periodic_reconcile)
        self.set_interval(600, self._check_updates)
        self.set_interval(600, self._audit_stats)  # Every 10 minutes
        # Initial update check after a brief delay
        self.set_timer(5, self._check_updates)
        self.query_one("#session-table", DataTable).focus()
        mlog("app", "started")
        t0 = _perf("on_mount: set_interval + focus + mlog", t0)

        # Teach the jumpback hotkey for the first 20 launches
        prefs = load_prefs()
        launches = prefs.get("launch_count", 0) + 1
        prefs["launch_count"] = launches
        save_prefs(prefs)
        if launches <= 20:
            self.notify(
                "Press [b]Ctrl+Shift+Space[/] from any app to return here "
                f"[dim]({21 - launches} more reminders)[/]",
                title="jumpback", timeout=6,
            )
        _perf("on_mount: launch_count save_prefs + notify", t0)

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
        cell = render_status_cell("working", self._spin_idx)
        for s in self._flat_rows:
            if s.status == "working":
                try:
                    table.update_cell(s.session_id, "status", cell)
                except Exception:
                    pass

    def _rebuild_table_columns(self) -> None:
        table = self.query_one("#session-table", DataTable)
        table.clear(columns=True)
        for col_key in self._visible_cols:
            info = ALL_COLUMNS.get(col_key, {})
            table.add_column(info.get("label", col_key), key=col_key)

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
            )

            # Hide closed sessions unless "All" is toggled
            if not self.show_archived:
                sessions = [s for s in sessions if s.status != "closed"]

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

            # When grouping, stable-sort by group key (preserves within-group
            # sort from the earlier sort_mode pass). Singletons collapse into
            # one "ungrouped" bucket at the bottom.
            if self.show_groups:
                groups: dict[str, list[Session]] = {}
                for s in flat:
                    groups.setdefault(_group_key(s.title), []).append(s)
                singles = [k for k, v in groups.items() if len(v) < 2]
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

            # Pre-render rows in background thread (Rich markup generation)
            visible_cols = self._visible_cols
            rendered = [(s, render_row(s, visible_cols)) for s in flat]

            # Post to main thread for UI update
            self.call_from_thread(
                self._refresh_apply, sessions, flat, rendered, cleaned,
            )
        finally:
            self._refresh_pending = False
            if self._refresh_queued:
                self._refresh_queued = False
                self.call_from_thread(self.refresh_sessions)

    def _refresh_apply(self, sessions: list[Session], flat: list[Session],
                       rendered: list[tuple[Session, list[str]]],
                       cleaned: list[str]) -> None:
        """Main thread: apply computed results to UI."""
        self.sessions = sessions

        if cleaned:
            self.notify(
                f"Auto-closed {len(cleaned)} debriefed session{'s' if len(cleaned) > 1 else ''}",
                timeout=3,
            )

        # Log status transitions
        for s in flat:
            if s.is_subagent:
                continue
            prev = self._prev_statuses.get(s.session_id)
            if prev and prev != s.status:
                mlog("status", "transition", sid=s.session_id[:12],
                     title=s.title, prev=prev, new=s.status)
            self._prev_statuses[s.session_id] = s.status

        table = self.query_one("#session-table", DataTable)
        # Snapshot cursor and scroll right before clear (user may have navigated
        # since refresh_sessions() dispatched the worker). Must read the OLD
        # _row_map here — cursor_row indexes the table as it was rendered.
        old_map = self._row_map
        cr = table.cursor_row
        selected_key = self._selected_key
        saved_row_idx = cr
        if cr is not None and cr < len(old_map):
            sel = old_map[cr]
            if sel:
                selected_key = sel.session_id
                saved_row_idx = None  # will restore by key instead

        self._flat_rows = flat
        saved_scroll_x = table.scroll_x
        saved_scroll_y = table.scroll_y

        table.clear()
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
            table.add_row(*cells, key=s.session_id)
            row_map.append(s)
        self._row_map = row_map
        self._group_header_rows = group_header_rows

        if saved_row_idx is None and selected_key:
            for idx, s in enumerate(row_map):
                if s and s.session_id == selected_key:
                    table.move_cursor(row=idx)
                    break
        elif saved_row_idx is not None:
            table.move_cursor(row=min(saved_row_idx, len(row_map) - 1))

        table.scroll_to(saved_scroll_x, saved_scroll_y, animate=False)
        self.query_one(StatsBar).update_stats(self.sessions, self.sort_mode)

    def _make_menu_handler(self, s: Session):
        """Build the SessionMenu dismiss callback for a session."""
        def handle_action(action: str | None) -> None:
            mlog("menu", "action", action=action, sid=s.session_id[:12],
                 title=s.title, status=s.status)
            if action == "jump":
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
                            self.notify(f"Resuming {s.title[:20]} in new tab", timeout=4)
                        else:
                            self.notify("Could not find or resume session", timeout=4)
                mlog("menu", "jump_result", sid=s.session_id[:12], success=ok)
            elif action == "edit_name":
                self.action_edit_name()
            elif action == "resume":
                ok = resume_session(s)
                if ok:
                    self.notify(f"Resuming {s.title[:20]}…", timeout=4)
                    global _pid_map_ts
                    _pid_map_ts = 0
                else:
                    self.notify("Could not open terminal", timeout=4)
                mlog("menu", "resume_result", sid=s.session_id[:12], success=ok)
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
        """Enter key pressed on a row — open context menu."""
        if not (event.row_key and event.row_key.value):
            return
        s = next((s for s in self._flat_rows if s.session_id == event.row_key.value), None)
        if not s:
            return
        self.push_screen(SessionMenu(s), self._make_menu_handler(s))

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

        if s.status in ("archived", "closed"):
            detail_parts.append(
                f"[dim]Project:[/] {s.project}  "
                f"[dim]Cost:[/] ${s.cost:.2f}  "
                f"[dim]Output:[/] {format_tokens(s.tokens_out)}  "
                f"[dim]Messages:[/] {s.message_count}"
            )
            detail_parts.append("[dim]Press Enter → Resume to continue this session[/]")

        tasks = load_tasks(s.session_id)
        if tasks:
            detail_parts.append(format_plan(tasks))
        elif s.last_assistant_text:
            preview = _escape_markup(s.last_assistant_text[:400])
            detail_parts.append(f"[italic]{preview}[/italic]")

        if not _get_api_key():
            detail_parts.append("[dim #D97757]Press K to add API key for haiku session summaries[/]")

        self.query_one("#detail-panel", Static).update("\n".join(detail_parts))

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

    def _dismiss_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        search_bar.value = ""
        search_bar.display = False
        self.query_one("#search-hint", Label).display = True
        self._filter = ""
        self.refresh_sessions()
        self.query_one("#session-table", DataTable).focus()

    def action_refresh(self) -> None:
        global _pid_map_ts
        _pid_map_ts = 0  # Force fresh PID map
        _scan_cache.clear()  # Force re-read all transcripts
        _subagent_cache.clear()
        threading.Thread(target=_reconcile_sessions, daemon=True).start()
        self._check_updates()
        # Don't clear _refresh_pending — that would dispatch a second worker
        # concurrently mutating the module-global caches. refresh_sessions()
        # queues a follow-up refresh if one is already in flight.
        self.refresh_sessions()
        self.notify("Refreshed", timeout=3)

    def action_cycle_sort(self) -> None:
        self.sort_mode = self.sort_mode.next()
        self._selected_key = None
        self.refresh_sessions()
        self.notify(f"Sort: {self.sort_mode.label}", timeout=3)

    def action_toggle_subagents(self) -> None:
        self.show_subagents = not self.show_subagents
        self.refresh_sessions()
        self.notify(f"Subagents {'shown' if self.show_subagents else 'hidden'}", timeout=3)

    def action_toggle_archived(self) -> None:
        self.show_archived = not self.show_archived
        self.refresh_sessions()
        self.notify(f"All sessions {'shown' if self.show_archived else 'recent only'}", timeout=3)

    def action_toggle_groups(self) -> None:
        self.show_groups = not self.show_groups
        self.refresh_sessions()
        self.notify(f"Grouping {'on' if self.show_groups else 'off'}", timeout=3)

    def action_edit_name(self) -> None:
        """Open inline prompt, then send /rename <name> to the selected session."""
        table = self.query_one(DataTable)
        cr = table.cursor_row
        if cr is None or cr >= len(self._row_map):
            return
        s = self._row_map[cr]
        if (s is None
                or s.status in ("archived", "closed")
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
                self.notify(f"Resuming {s.title[:20]} in new tab", timeout=4)
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
            and s.status not in ("archived", "closed")
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
        if s.status in ("archived", "closed"):
            self.notify("Session not running", timeout=3)
            return
        self._do_rename(s, "action")

    _view_modes = ["rows", "kanban", "timeline"]
    _current_view = "rows"

    def action_cycle_view(self) -> None:
        idx = self._view_modes.index(self._current_view)
        next_mode = self._view_modes[(idx + 1) % len(self._view_modes)]
        self._open_view(next_mode)

    def _open_view(self, mode: str) -> None:
        self._current_view = mode
        if mode == "kanban":
            def on_kanban_dismiss(result: str | None) -> None:
                if result == "__next_view__":
                    self._open_view("timeline")
                else:
                    self._current_view = "rows"
            self.push_screen(
                KanbanView(self.sessions, by_group=self.show_groups),
                on_kanban_dismiss,
            )
        elif mode == "timeline":
            def on_timeline_dismiss(result: str | None) -> None:
                if result == "__next_view__":
                    self._open_view("rows")
                else:
                    self._current_view = "rows"
            self.push_screen(
                TimelineView(self.sessions, by_group=self.show_groups),
                on_timeline_dismiss,
            )
        # mode == "rows" → just stay on the default screen (no modal to push)

    def action_restart(self) -> None:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=_REPO_DIR, capture_output=True, timeout=15,
        )
        self.exit(return_code=RESTART_EXIT_CODE)

    def action_toggle_theme(self) -> None:
        self.theme = "gruvbox-light" if "dark" in self.theme else "gruvbox-dark"
        prefs = load_prefs()
        prefs["theme"] = self.theme
        save_prefs(prefs)
        self.notify(f"Theme: {self.theme.replace('gruvbox-', '')}", timeout=2)

    def _group_header_indices(self) -> list[int]:
        """Return row indices of group header rows (not spacers)."""
        return list(self._group_header_rows)

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
                prefs = load_prefs()
                prefs["columns"] = cols
                prefs["column_order"] = self._col_order
                save_prefs(prefs)
                self._rebuild_table_columns()
                self.refresh_sessions()
                self.notify("Columns updated", timeout=3)

        self.push_screen(picker, on_dismiss)

    def action_statusline_config(self) -> None:
        sl_prefs = load_statusline_prefs()
        screen = StatuslineConfig(sl_prefs)

        def on_dismiss(result: dict[str, bool] | None) -> None:
            if result is not None:
                prefs = load_prefs()
                prefs["statusline"] = result
                save_prefs(prefs)
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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--log":
        from monitor_log import tail_log
        cat_filter = sys.argv[2] if len(sys.argv) > 2 else None
        tail_log(category=cat_filter)
    else:
        app = ClaudeMonitor()
        app.run()
        if app.return_code == RESTART_EXIT_CODE:
            os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
