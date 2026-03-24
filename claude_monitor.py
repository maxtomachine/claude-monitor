#!/usr/bin/env python3
"""Claude Code session monitor — btop-style TUI."""

import json
import os
import re
import signal
import subprocess
import sys
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


CLAUDE_DIR = Path.home() / ".claude" / "projects"
SIGNALS_DIR = Path.home() / ".claude" / "session-signals"
HOOK_STATE_DIR = Path.home() / ".claude" / "session-states"
TASKS_DIR = Path.home() / ".claude" / "tasks"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PREFS_PATH = Path.home() / ".claude" / "monitor-prefs.json"
DOING_MAX_WIDTH = 40
RESTART_EXIT_CODE = 99

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
    "status":    {"label": "Status",   "default": True},
    "session":   {"label": "Session",  "default": True},
    "project":   {"label": "Project",  "default": True},
    "model":     {"label": "Model",    "default": True},
    "context":   {"label": "Context",  "default": True},
    "compact":   {"label": "Compacts", "default": True},
    "tokens":    {"label": "Tokens",   "default": True},
    "cost":      {"label": "Cost",     "default": True},
    "mcp":       {"label": "MCP",      "default": False},
    "msgs":      {"label": "Msgs",     "default": False},
    "duration":  {"label": "Duration", "default": False},
    "active":    {"label": "Active",   "default": True},
    "doing":     {"label": "Doing",    "default": True},
}


class SortMode(Enum):
    ACTIVITY = "activity"
    STATUS = "status"
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
            SortMode.CONTEXT: "Context %", SortMode.TOKENS: "Tokens",
            SortMode.COST: "Cost",
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


def parse_sessions(include_archived: bool = False) -> list[Session]:
    sessions = []
    now = time.time()
    active_cutoff = now - 86400
    archive_cutoff = now - 86400 * 7  # 7 days for archived

    if not CLAUDE_DIR.exists():
        return sessions

    _gc_state_files()

    meta = load_index_metadata()

    for jsonl_path in CLAUDE_DIR.rglob("*.jsonl"):
        if "subagents" in str(jsonl_path):
            continue
        try:
            mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue

        is_archived = mtime < active_cutoff
        if is_archived and not include_archived:
            continue
        if mtime < archive_cutoff:
            continue

        session_id = jsonl_path.stem
        idx = meta.get(session_id, {})
        project = idx.get("project", jsonl_path.parent.name.split("-")[-1] or "~")

        session = build_session(str(jsonl_path), session_id, project, idx, mtime)
        if session:
            # Hide ghost sessions: ≤20 output tokens = just the greeting
            # Keep only if we can confirm a live process
            if session.tokens_out <= 20:
                if _is_session_alive(session_id) is not True:
                    continue
            if is_archived:
                session.status = "archived"
            session.compact_count = count_compactions(str(jsonl_path))
            if not is_archived:
                for sub_path in find_subagent_paths(str(jsonl_path)):
                    sub = build_session(
                        str(sub_path), sub_path.stem, project, {},
                        sub_path.stat().st_mtime, is_subagent=True, parent_id=session_id,
                    )
                    if sub:
                        session.subagents.append(sub)
            sessions.append(session)

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
        context_pct = 100 - remaining
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
        return False

    # No PID file — check if we just resumed this session
    resumed_at = _recently_resumed.get(session_id)
    if resumed_at and (time.time() - resumed_at) < _RESUME_GRACE:
        return True

    _recently_resumed.pop(session_id, None)
    return False


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
    for name in [
        _read_session_cache("name", session.session_id),
        (read_hook_state(session.session_id) or {}).get("title", ""),
        session.status_name,
        session.title,
        Path(session.cwd).name if session.cwd else "",
    ]:
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
            for (const cand of candidates) {{
                const match = allTitles.find(t => t.includes(cand));
                if (match) {{ targetName = match; matchedCand = cand; break; }}
            }}
            if (!targetName) continue;

            app.activate();
            delay(0.1);

            const proc = se.processes.byName(appName);

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
                const item = items.find(n => n && n.includes(matchedCand));
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
        return out.startswith("matched:")
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


def _send_to_terminal_session(session: Session, text: str) -> bool:
    """Raise the session's terminal and type text + Enter."""
    return _raise_window_by_content(session, then_text=text)


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
    return "/" + name[1:].replace("-", "/")


def resume_session(session: Session, then_command: str = "") -> bool:
    """Resume a Claude session in a new Ghostty tab (falls back to Terminal.app).

    If then_command is set (e.g. "/rename"), the resume waits for Claude to
    start and then sends that command via keystroke.
    """
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
         cwd=cwd, jsonl_exists=jsonl_exists, then=then_command or None)
    if not jsonl_exists:
        mlog("resume", "no_jsonl", sid=session.session_id[:12],
             path=session.transcript_path)
        return False

    # Ghostty: open new tab via keystroke, then type the command
    jxa = f"""(() => {{
        const se = Application("System Events");
        const cwd = {json.dumps(cwd)};
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


class KanbanView(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("k", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
        Binding("up", "move('up')", "↑", show=False),
        Binding("down", "move('down')", "↓", show=False),
        Binding("left", "move('left')", "←", show=False),
        Binding("right", "move('right')", "→", show=False),
        Binding("enter", "select", "Select"),
    ]
    CSS = """
    KanbanView { background: $background; align: center middle; }
    #kanban-outer {
        width: 100%; height: 100%; padding: 1 2;
    }
    #kanban-title { text-align: center; text-style: bold; padding-bottom: 1; }
    #kanban-board {
        width: 100%; height: 1fr;
    }
    .kanban-col {
        width: 1fr; height: 100%; padding: 0 1;
        border-right: solid $panel;
    }
    .kanban-col:last-child { border-right: none; }
    .kanban-col-header {
        text-align: center; text-style: bold; padding-bottom: 1;
        height: 2;
    }
    .kanban-cards {
        height: 1fr; overflow-y: auto;
    }
    .kanban-card {
        height: auto; padding: 0 1; margin-bottom: 1;
        background: $panel; border: solid $primary-darken-2;
    }
    .kanban-card.-selected {
        border: thick $accent;
        background: $boost;
    }
    .kanban-empty { color: $text-disabled; text-align: center; padding-top: 2; }
    """

    def __init__(self, sessions: list[Session]) -> None:
        super().__init__()
        self._spin_idx = 0
        valid = {k for k, _, _ in KANBAN_COLUMNS}
        # grid[col] = [(session, body_text), ...] — body precomputed, icon swapped on tick
        self._grid: list[list[tuple[Session, str]]] = [[] for _ in KANBAN_COLUMNS]
        col_idx = {k: i for i, (k, _, _) in enumerate(KANBAN_COLUMNS)}
        for s in sessions:
            if s.is_subagent:
                continue
            bucket = s.status if s.status in valid else "closed"
            title = _escape_markup(s.title.replace("-", "-\n"))
            activity = generate_activity(s)
            body = f"{title}[/]"
            if activity:
                body += f"\n[dim]{_escape_markup(activity[:36])}[/]"
            self._grid[col_idx[bucket]].append((s, body))
        self._col = next((i for i, c in enumerate(self._grid) if c), 0)
        self._row = 0
        self._working_col = col_idx.get("working", -1)

    def _card_text(self, col_idx: int, body: str) -> str:
        status_key = KANBAN_COLUMNS[col_idx][0]
        if status_key == "working":
            icon = SPINNER_FRAMES[self._spin_idx % len(SPINNER_FRAMES)]
            return f"[bold][#D97757]{icon}[/#D97757] {body}"
        icon = STATUS_ICON.get(status_key, "·")
        return f"[bold]{icon} {body}"

    def on_mount(self) -> None:
        wc = self._working_col
        if wc >= 0 and self._grid[wc]:
            self.set_interval(0.132, self._tick_spinner)

    def _tick_spinner(self) -> None:
        self._spin_idx += 1
        wc = self._working_col
        for row_idx, (_, body) in enumerate(self._grid[wc]):
            try:
                card = self.query_one(f"#kc-{wc}-{row_idx}", Static)
                card.update(self._card_text(wc, body))
            except Exception:
                pass

    def compose(self) -> ComposeResult:
        with Vertical(id="kanban-outer"):
            yield Label("[bold]Kanban Board[/]  [dim]←→↑↓ nav · enter select · esc close[/]", id="kanban-title")
            with Horizontal(id="kanban-board"):
                for col_idx, (key, header, color) in enumerate(KANBAN_COLUMNS):
                    with Vertical(classes="kanban-col"):
                        count = len(self._grid[col_idx])
                        yield Label(
                            f"[{color}]{header}[/] [dim]({count})[/]",
                            classes="kanban-col-header",
                        )
                        with Vertical(classes="kanban-cards"):
                            if not self._grid[col_idx]:
                                yield Static("[dim]—[/]", classes="kanban-empty")
                            for row_idx, (_, body) in enumerate(self._grid[col_idx]):
                                sel = col_idx == self._col and row_idx == self._row
                                classes = "kanban-card -selected" if sel else "kanban-card"
                                yield Static(self._card_text(col_idx, body),
                                             classes=classes,
                                             id=f"kc-{col_idx}-{row_idx}")

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


class SessionMenu(ModalScreen[str]):
    BINDINGS = [
        Binding("escape", "dismiss_menu", "Close"),
        Binding("q", "dismiss_menu", "Close", show=False),
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

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        s = self.session
        options = []
        if s.status in ("archived", "closed"):
            options.append(Option("▶   Resume session", id="resume"))
        else:
            options.append(Option("🖥   Jump to terminal", id="jump"))
            options.append(Option("🏷   Send /rename", id="rename"))
        options.append(Option(f"📋  Copy session ID ({s.session_id[:8]}…)", id="copy_id"))
        if s.remote_url:
            options.append(Option("🔗  Open remote control", id="remote"))
        options.append(Option("📂  Open transcript", id="transcript"))
        # Debrief requires a /debrief skill installed on the user's machine
        # if s.status not in ("archived", "closed", "debriefing"):
        #     options.append(Option("📝  Debrief & close", id="dismiss"))
        if s.status not in ("archived", "closed"):
            options.append(Option("❌  Kill process", id="kill"))
        options.append(Option("─" * 26, id="sep", disabled=True))
        options.append(Option("◀   Back", id="close"))

        with Vertical(id="menu-container"):
            yield Label(f"[bold]{s.title}[/]", id="menu-title")
            yield OptionList(*options, id="menu-options")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_menu(self) -> None:
        self.dismiss("close")


class ColumnPicker(ModalScreen[list[str]]):
    BINDINGS = [
        Binding("escape", "done", "Done"),
        Binding("enter", "toggle_col", "Toggle"),
        Binding("space", "toggle_col", "Toggle", show=False),
        Binding("shift+up", "move_up", "Move Up", show=False),
        Binding("shift+down", "move_down", "Move Down", show=False),
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
            yield Label("[dim]Enter/Space toggle · Shift+↑↓ reorder · Esc done[/]", id="picker-hint")

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


class StatsBar(Horizontal):
    def compose(self) -> ComposeResult:
        yield Label("", id="stats-working")
        yield Label("", id="stats-waiting")
        yield Label("", id="stats-idle")
        yield Label("", id="stats-closed")
        yield Label("", id="stats-total-cost")
        yield Label("", id="stats-sort")
        yield Input(placeholder="🔍 filter...", id="search-bar")

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
        Binding("z", "toggle_archived", "All"),
        Binding("c", "pick_columns", "Columns"),
        # Binding("l", "statusline_config", "Statusline"),  # TODO: re-enable after statusline merge
        Binding("d", "toggle_debug", "Debug"),
        Binding("slash", "start_search", "Search"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("k", "kanban", "Kanban"),
        Binding("t", "toggle_theme", "Theme", show=False),
        Binding("R", "restart", "Restart", show=False),
        Binding("j", "cursor_down", "↓", show=False),
        Binding("n", "rename_selected", "Rename", show=False),
    ]

    sort_mode: reactive[SortMode] = reactive(SortMode.ACTIVITY)
    show_subagents: reactive[bool] = reactive(False)
    show_archived: reactive[bool] = reactive(False)
    debug_logging: reactive[bool] = reactive(True)  # ON by default
    sessions: list[Session] = []
    _flat_rows: list[Session] = []
    _selected_key: str | None = None
    _visible_cols: list[str] = []
    _col_order: list[str] = []
    _filter: str = ""
    _dismissing_sessions: dict[str, str] = {}  # sid -> "debriefing" | "closing"
    _dismiss_failed: set[str] = set()  # sids where dismiss failed (can't reach terminal)
    _prev_statuses: dict[str, str] = {}  # sid -> previous status (for transition logging)
    _spin_idx: int = 0

    def notify(self, message, *, timeout: float | None = 5, **kwargs):
        """Override to log every toast notification."""
        mlog("toast", "notify", message=str(message))
        super().notify(message, timeout=timeout, **kwargs)

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatsBar()
        yield DataTable(id="session-table", cursor_type="row")
        yield Static(
            "[dim]↑↓/jk navigate · [bold]Enter[/] menu · "
            "[bold]s[/] sort · [bold]a[/] agents · "
            "[bold]z[/] all · [bold]c[/] columns · "
            "[bold]d[/] debug · [bold]/[/] search[/]",
            id="detail-panel"
        )
        yield Footer()

    def on_mount(self) -> None:
        self._visible_cols = get_visible_columns()
        self._col_order = get_column_order()
        saved_theme = load_prefs().get("theme")
        if saved_theme:
            self.theme = saved_theme
        self._rebuild_table_columns()
        # Set terminal window title (skip in tests / non-tty)
        if sys.stdout.isatty() and "PYTEST_CURRENT_TEST" not in os.environ:
            print("\033]2;Claude Monitor\007", end="", flush=True)
        self.refresh_sessions()
        self.set_interval(3, self.refresh_sessions)
        self.set_interval(0.132, self._tick_spinner)
        self.set_interval(600, self._audit_stats)  # Every 10 minutes
        self.query_one("#session-table", DataTable).focus()
        mlog("app", "started")

    def _tick_spinner(self) -> None:
        """Advance spinner frame and update only working-status cells."""
        if "status" not in self._visible_cols:
            return
        self._spin_idx += 1
        table = self.query_one("#session-table", DataTable)
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

    def refresh_sessions(self) -> None:
        """Schedule a refresh — heavy work runs in a background thread."""
        if self._refresh_pending:
            return  # Previous refresh still running, skip
        self._refresh_pending = True
        # Snapshot UI state before background work
        table = self.query_one("#session-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self._flat_rows):
            self._selected_key = self._flat_rows[table.cursor_row].session_id
        self.run_worker(
            lambda: self._refresh_compute(),
            thread=True,
        )

    def _refresh_compute(self) -> None:
        """Background thread: parse, sort, filter — no UI access."""
        try:
            sessions = parse_sessions(include_archived=self.show_archived)

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

            # Pre-render rows in background thread (Rich markup generation)
            visible_cols = self._visible_cols
            rendered = [(s, render_row(s, visible_cols)) for s in flat]

            # Post to main thread for UI update
            self.call_from_thread(
                self._refresh_apply, sessions, flat, rendered, cleaned,
            )
        finally:
            self._refresh_pending = False

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

        self._flat_rows = flat

        table = self.query_one("#session-table", DataTable)
        # Snapshot cursor and scroll right before clear (user may have navigated
        # since refresh_sessions() dispatched the worker)
        if table.cursor_row is not None and table.cursor_row < len(flat):
            selected_key = self._flat_rows[table.cursor_row].session_id if table.cursor_row < len(self._flat_rows) else self._selected_key
        else:
            selected_key = self._selected_key
        saved_scroll_x = table.scroll_x
        saved_scroll_y = table.scroll_y

        table.clear()
        for s, cells in rendered:
            table.add_row(*cells, key=s.session_id)

        if selected_key:
            for idx, s in enumerate(flat):
                if s.session_id == selected_key:
                    table.move_cursor(row=idx)
                    break

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
                    ok = resume_session(s)
                    if ok:
                        self.notify(f"Resuming {s.title[:20]} in new tab", timeout=4)
                    else:
                        self.notify("Could not find or resume session", timeout=4)
                mlog("menu", "jump_result", sid=s.session_id[:12], success=ok)
            elif action == "rename":
                self._do_rename(s, "menu")
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
                    try:
                        os.kill(pid, signal.SIGTERM)
                        mlog("menu", "kill", sid=s.session_id[:12], pid=pid)
                        self.notify(f"Killed {s.title[:20]} (PID {pid})", timeout=4)
                    except OSError as e:
                        mlog("menu", "kill_error", sid=s.session_id[:12], error=str(e))
                        self.notify(f"Kill failed: {e}", timeout=4)
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
        s = next((s for s in self._flat_rows if s.session_id == event.row_key.value), None)
        if not s:
            return

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
        search_bar.focus()

    def action_clear_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        if search_bar.display or self._filter:
            self._dismiss_search()

    def _dismiss_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        search_bar.value = ""
        search_bar.display = False
        self._filter = ""
        self.refresh_sessions()
        self.query_one("#session-table", DataTable).focus()

    def action_refresh(self) -> None:
        global _pid_map_ts
        _pid_map_ts = 0  # Force fresh PID map
        _scan_cache.clear()  # Force re-read all transcripts
        _subagent_cache.clear()
        self._refresh_pending = False  # Allow immediate refresh
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

    def action_rename_selected(self) -> None:
        """Send /rename to the currently selected session's terminal."""
        table = self.query_one(DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self._flat_rows):
            return
        s = self._flat_rows[table.cursor_row]
        if s.status in ("archived", "closed"):
            self.notify("Session not running", timeout=3)
            return
        self._do_rename(s, "action")

    def action_kanban(self) -> None:
        self.push_screen(KanbanView(self.sessions))

    def action_restart(self) -> None:
        self.exit(return_code=RESTART_EXIT_CODE)

    def action_toggle_theme(self) -> None:
        self.theme = "textual-light" if "dark" in self.theme else "textual-dark"
        prefs = load_prefs()
        prefs["theme"] = self.theme
        save_prefs(prefs)
        self.notify(f"Theme: {self.theme.replace('textual-', '')}", timeout=2)

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
