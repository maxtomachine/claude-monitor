#!/usr/bin/env python3
"""Claude Code session monitor — btop-style TUI."""

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

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
PREFS_PATH = Path.home() / ".claude" / "monitor-prefs.json"

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


STATUS_PRIORITY = {"working": 0, "needs_approval": 1, "waiting": 2, "idle": 3}
STATUS_DISPLAY = {
    "working": ("● WORKING", "green"),
    "needs_approval": ("◉ APPROVE", "yellow"),
    "waiting": ("○ WAITING", "dark_orange"),
    "idle": ("◌ IDLE", "dim"),
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


# ── Data parsing ──────────────────────────────────────────────────────────────


def parse_timestamp(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def scan_full_file(path: str) -> dict:
    """Single-pass full file scan: tokens, MCP, title, slug, created, last activity."""
    result = {
        "custom_title": "", "slug": "", "mcp_calls": 0,
        "tokens_in": 0, "tokens_out": 0,
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
                    if '"slug"' in line and not result["slug"]:
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
                if msg.get("slug") and not result["slug"]:
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
                                result["last_assistant_text"] = text[:120]

                result["message_count"] += 1

    except OSError:
        pass

    return result


def count_compactions(parent_path: str) -> int:
    parent = Path(parent_path)
    subagent_dir = parent.parent / parent.stem / "subagents"
    if not subagent_dir.exists():
        return 0
    return len(list(subagent_dir.glob("agent-acompact-*.jsonl")))


def find_subagent_paths(parent_path: str) -> list[Path]:
    parent = Path(parent_path)
    subagent_dir = parent.parent / parent.stem / "subagents"
    if not subagent_dir.exists():
        return []
    return sorted(subagent_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


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
                }
    return meta


def estimate_cost(model_id: str, tokens_in: int, tokens_out: int) -> float:
    for k, (ip, op) in MODEL_PRICING.items():
        if k in model_id:
            return (tokens_in / 1_000_000 * ip) + (tokens_out / 1_000_000 * op)
    return 0.0


def parse_sessions() -> list[Session]:
    sessions = []
    cutoff = time.time() - 86400

    if not CLAUDE_DIR.exists():
        return sessions

    meta = load_index_metadata()

    for jsonl_path in CLAUDE_DIR.rglob("*.jsonl"):
        if "subagents" in str(jsonl_path):
            continue
        try:
            mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue

        session_id = jsonl_path.stem
        idx = meta.get(session_id, {})
        project = idx.get("project", jsonl_path.parent.name.split("-")[-1] or "~")

        session = build_session(str(jsonl_path), session_id, project, idx, mtime)
        if session:
            session.compact_count = count_compactions(str(jsonl_path))
            for sub_path in find_subagent_paths(str(jsonl_path)):
                sub = build_session(
                    str(sub_path), sub_path.stem, project, {},
                    sub_path.stat().st_mtime, is_subagent=True, parent_id=session_id,
                )
                if sub:
                    session.subagents.append(sub)
            sessions.append(session)

    return sessions


def build_session(path: str, session_id: str, project: str, idx: dict,
                  mtime: float, is_subagent: bool = False,
                  parent_id: str = "") -> Session | None:
    data = scan_full_file(path)

    status = determine_status(session_id, data["last_assistant_time"])

    total_tokens = data["tokens_in"] + data["tokens_out"]
    if total_tokens == 0:
        context_pct = 100
    else:
        used = min(total_tokens, 200000)
        context_pct = max(0, 100 - int((used / 200000) * 100))

    cost = estimate_cost(data["model_id"], data["tokens_in"], data["tokens_out"])

    if is_subagent:
        parts = Path(path).stem.split("-")
        display_title = "-".join(parts[:2]) if len(parts) >= 2 else session_id[:12]
    else:
        # Priority: custom title > index summary > first prompt > cwd
        display_title = (
            data["custom_title"]
            or idx.get("summary", "")
            or idx.get("firstPrompt", "")[:60]
            or Path(data["cwd"]).name
            or session_id[:8]
        )

    remote_url = ""
    slug = data["slug"]
    if slug and not is_subagent:
        remote_url = f"https://claude.ai/code/session_{slug}"

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
    )


def determine_status(session_id: str, last_assistant_time: float) -> str:
    if SIGNALS_DIR.exists():
        signal_file = SIGNALS_DIR / session_id
        if signal_file.exists():
            try:
                s = signal_file.read_text().strip()
                return {"working": "working", "permission": "needs_approval",
                        "stop": "waiting"}.get(s, "idle")
            except OSError:
                pass
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
    filled = round(pct / 100 * width)
    empty = width - filled
    if pct < 25:
        color = "red"
    elif pct < 50:
        color = "yellow"
    elif pct < 75:
        color = "green"
    else:
        color = "bright_green"
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
    if cost >= 10:
        return f"[red]${cost:.2f}[/]"
    elif cost >= 5:
        return f"[yellow]${cost:.2f}[/]"
    elif cost > 0:
        return f"${cost:.2f}"
    return "[dim]—[/]"


def _to_gerund(verb: str) -> str:
    """Convert a base verb to gerund form."""
    verb = verb.lower()
    if verb.endswith("ing"):
        return verb.capitalize()
    if verb.endswith("e") and not verb.endswith("ee"):
        return (verb[:-1] + "ing").capitalize()
    if re.match(r'.*[^aeiou][aeiou][^aeiouwy]$', verb) and len(verb) <= 5:
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
    # Build the base gerund from tool call or assistant text
    gerund = ""
    if s.last_tool:
        gerund = _gerund_from_tool(s.last_tool, s.last_tool_input)
    if not gerund and s.last_assistant_text:
        gerund = _gerund_from_text(s.last_assistant_text) or ""

    if not gerund:
        return ""

    # Apply status-based transformation
    if s.status == "needs_approval":
        return f"Awaiting approval"
    elif s.status == "idle":
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
        return sorted(sessions, key=lambda s: s.tokens_in + s.tokens_out, reverse=True)
    elif mode == SortMode.COST:
        return sorted(sessions, key=lambda s: s.cost, reverse=True)
    return sessions


# ── Column rendering ──────────────────────────────────────────────────────────


def render_row(s: Session, visible_cols: list[str]) -> list[str]:
    cells = []
    for col in visible_cols:
        if col == "status":
            icon, color = STATUS_DISPLAY.get(s.status, ("?", "white"))
            cells.append(f"[{color}]{icon}[/]")
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
            cells.append(format_context_bar(s.context_pct))
        elif col == "compact":
            cells.append(format_compactions(s.compact_count))
        elif col == "tokens":
            cells.append(format_tokens(s.tokens_in + s.tokens_out))
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
                activity_escaped = activity.replace("[", "\\[").replace("]", "\\]")
                if s.status == "idle":
                    cells.append(f"[dim]{activity_escaped}[/]")
                elif s.status == "needs_approval":
                    cells.append(f"[yellow]{activity_escaped}[/]")
                else:
                    cells.append(activity_escaped)
            else:
                cells.append("[dim]—[/]")
    return cells


# ── Ghostty tab focus ─────────────────────────────────────────────────────────


def find_ghostty_tab_for_cwd(target_cwd: str) -> int | None:
    """Find a Ghostty tab whose shell is in the target cwd."""
    try:
        # Get all Ghostty child process PIDs
        result = subprocess.run(
            ["pgrep", "-P", "1", "-f", "ghostty"],
            capture_output=True, text=True, timeout=2,
        )
        ghostty_pids = result.stdout.strip().splitlines()

        # Find shell processes under ghostty
        for gp in ghostty_pids:
            try:
                children = subprocess.run(
                    ["pgrep", "-P", gp.strip()],
                    capture_output=True, text=True, timeout=2,
                )
                for child_pid in children.stdout.strip().splitlines():
                    # Check cwd of child process
                    cwd_result = subprocess.run(
                        ["lsof", "-p", child_pid.strip(), "-Fn"],
                        capture_output=True, text=True, timeout=2,
                    )
                    for lsof_line in cwd_result.stdout.splitlines():
                        if lsof_line.startswith("n") and target_cwd in lsof_line:
                            return int(child_pid.strip())
            except (subprocess.TimeoutExpired, ValueError):
                continue
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def focus_ghostty_session(session: Session) -> None:
    """Focus Ghostty and attempt to find the correct tab."""
    # First just activate Ghostty
    script = 'tell application "Ghostty" to activate'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # TODO: Ghostty doesn't yet expose per-tab focus via AppleScript.
    # When ghostty +list-surfaces or the AppleScript API matures,
    # we can match by cwd and focus the exact tab.


def copy_to_clipboard(text: str) -> None:
    try:
        subprocess.run(["pbcopy"], input=text.encode(), timeout=2)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


# ── Screens ───────────────────────────────────────────────────────────────────


class SessionMenu(ModalScreen[str]):
    BINDINGS = [
        Binding("escape", "dismiss_menu", "Close"),
        Binding("q", "dismiss_menu", "Close", show=False),
    ]
    CSS = """
    SessionMenu { align: center middle; }
    #menu-container {
        width: 50; max-height: 20;
        background: $surface; border: solid $primary; padding: 1 2;
    }
    #menu-title { text-align: center; text-style: bold; padding-bottom: 1; }
    """

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        s = self.session
        options = [
            Option("🖥  Jump to terminal", id="jump"),
            Option(f"📋  Copy session ID ({s.session_id[:8]}…)", id="copy_id"),
        ]
        if s.remote_url:
            options.append(Option("🔗  Open remote control", id="remote"))
        options.append(Option("📂  Reveal transcript", id="transcript"))
        options.append(Option("─" * 30, id="sep", disabled=True))
        options.append(Option("❌  Close", id="close"))

        with Vertical(id="menu-container"):
            yield Label(f"[bold]{s.title}[/]", id="menu-title")
            yield OptionList(*options, id="menu-options")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_menu(self) -> None:
        self.dismiss("close")


class ColumnPicker(ModalScreen[list[str]]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Save"),
    ]
    CSS = """
    ColumnPicker { align: center middle; }
    #picker-container {
        width: 40; max-height: 24;
        background: $surface; border: solid $primary; padding: 1 2;
    }
    #picker-title { text-align: center; text-style: bold; padding-bottom: 1; }
    #picker-hint { text-align: center; padding-top: 1; }
    """

    def __init__(self, visible: list[str]) -> None:
        super().__init__()
        self.visible = set(visible)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-container"):
            yield Label("[bold]Column Picker[/]", id="picker-title")
            for key, info in ALL_COLUMNS.items():
                yield Checkbox(info["label"], value=key in self.visible, id=f"col-{key}")
            yield Label("[dim]Enter to save · Escape to cancel[/]", id="picker-hint")

    def action_confirm(self) -> None:
        cols = [k for k in ALL_COLUMNS if self.query_one(f"#col-{k}", Checkbox).value]
        self.dismiss(cols)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Main App ──────────────────────────────────────────────────────────────────


class StatsBar(Horizontal):
    def compose(self) -> ComposeResult:
        yield Label("", id="stats-working")
        yield Label("", id="stats-waiting")
        yield Label("", id="stats-idle")
        yield Label("", id="stats-total-cost")
        yield Label("", id="stats-sort")

    def update_stats(self, sessions: list[Session], sort_mode: SortMode) -> None:
        working = sum(1 for s in sessions if s.status == "working")
        waiting = sum(1 for s in sessions if s.status in ("waiting", "needs_approval"))
        idle = sum(1 for s in sessions if s.status == "idle")
        total_cost = sum(s.cost for s in sessions)

        self.query_one("#stats-working", Label).update(f" [green]● {working} working[/]  ")
        self.query_one("#stats-waiting", Label).update(f" [dark_orange]○ {waiting} waiting[/]  ")
        self.query_one("#stats-idle", Label).update(f" [dim]◌ {idle} idle[/]  ")
        self.query_one("#stats-total-cost", Label).update(f" [cyan]Σ ${total_cost:.2f}[/]  ")
        self.query_one("#stats-sort", Label).update(f" [magenta]sort: {sort_mode.label}[/]")


class ClaudeMonitor(App):
    TITLE = "Claude Monitor"
    CSS = """
    Screen { background: $surface; }
    StatsBar {
        height: 1; padding: 0 1; background: $boost; dock: top;
    }
    StatsBar Label { width: auto; }
    #session-table { height: 1fr; }
    #detail-panel {
        height: 7; padding: 0 2;
        background: $boost; dock: bottom; border-top: solid $primary;
    }
    #search-bar {
        height: 1; dock: bottom; display: none;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "open_menu", "Actions"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("t", "toggle_subagents", "Tree"),
        Binding("c", "pick_columns", "Columns"),
        Binding("p", "pause_all", "Pause All"),
        Binding("slash", "start_search", "Search"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("j", "cursor_down", "↓", show=False),
        Binding("k", "cursor_up", "↑", show=False),
    ]

    sort_mode: reactive[SortMode] = reactive(SortMode.ACTIVITY)
    show_subagents: reactive[bool] = reactive(False)
    sessions: list[Session] = []
    _flat_rows: list[Session] = []
    _selected_key: str | None = None
    _visible_cols: list[str] = []
    _filter: str = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatsBar()
        yield DataTable(id="session-table", cursor_type="row")
        yield Input(placeholder="Filter sessions...", id="search-bar")
        yield Static(
            "[dim]↑↓/jk navigate · [bold]Enter[/] actions · "
            "[bold]s[/] sort · [bold]t[/] tree · "
            "[bold]c[/] columns · [bold]p[/] pause · "
            "[bold]/[/] search[/]",
            id="detail-panel"
        )
        yield Footer()

    def on_mount(self) -> None:
        self._visible_cols = get_visible_columns()
        self._rebuild_table_columns()
        self.refresh_sessions()
        self.set_interval(3, self.refresh_sessions)

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

    def refresh_sessions(self) -> None:
        table = self.query_one("#session-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self._flat_rows):
            self._selected_key = self._flat_rows[table.cursor_row].session_id

        self.sessions = parse_sessions()
        filtered = self._filter_sessions(self.sessions)
        sorted_sessions = sort_sessions(filtered, self.sort_mode)

        flat: list[Session] = []
        for s in sorted_sessions:
            flat.append(s)
            if self.show_subagents and s.subagents:
                for sub in s.subagents:
                    flat.append(sub)
        self._flat_rows = flat

        table.clear()
        for s in flat:
            cells = render_row(s, self._visible_cols)
            table.add_row(*cells, key=s.session_id)

        if self._selected_key:
            for idx, s in enumerate(flat):
                if s.session_id == self._selected_key:
                    table.move_cursor(row=idx)
                    break

        self.query_one(StatsBar).update_stats(self.sessions, self.sort_mode)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not (event.row_key and event.row_key.value):
            return
        s = next((s for s in self._flat_rows if s.session_id == event.row_key.value), None)
        if not s:
            return

        icon, color = STATUS_DISPLAY.get(s.status, ("?", "white"))
        remote = f"\n[dim]remote:[/] {s.remote_url}" if s.remote_url else ""
        compact_info = f"  [dim]compactions:[/] {s.compact_count}" if s.compact_count else ""
        mcp_info = f"  [dim]MCP:[/] {s.mcp_calls}" if s.mcp_calls else ""

        activity = generate_activity(s)
        activity_line = f"\n[dim]doing:[/] {activity}" if activity else ""
        text_preview = ""
        if s.last_assistant_text:
            preview = s.last_assistant_text[:100].replace("[", "\\[").replace("]", "\\]")
            text_preview = f"\n[dim]said:[/] [italic]{preview}[/italic]"

        dur = format_duration(s.created, s.last_activity)

        self.query_one("#detail-panel", Static).update(
            f"[bold]{s.title}[/] [{color}]{icon}[/]  "
            f"[dim]id:[/] {s.session_id[:12]}  "
            f"[dim]cost:[/] ${s.cost:.2f}  "
            f"[dim]duration:[/] {dur}\n"
            f"[dim]dir:[/] {s.cwd}\n"
            f"[dim]tokens:[/] {format_tokens(s.tokens_in)} in / "
            f"{format_tokens(s.tokens_out)} out  "
            f"[dim]model:[/] {s.model}"
            f"{compact_info}{mcp_info}"
            f"{activity_line}{text_preview}"
            f"{remote}"
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-bar":
            self._filter = event.value
            self._selected_key = None
            self.refresh_sessions()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-bar":
            search_bar = self.query_one("#search-bar", Input)
            search_bar.display = False
            self.query_one("#session-table", DataTable).focus()

    def action_start_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        search_bar.display = True
        search_bar.focus()

    def action_clear_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        if search_bar.display:
            search_bar.value = ""
            search_bar.display = False
            self._filter = ""
            self.refresh_sessions()
            self.query_one("#session-table", DataTable).focus()

    def action_refresh(self) -> None:
        self.refresh_sessions()
        self.notify("Refreshed", timeout=1)

    def action_cycle_sort(self) -> None:
        self.sort_mode = self.sort_mode.next()
        self._selected_key = None
        self.refresh_sessions()
        self.notify(f"Sort: {self.sort_mode.label}", timeout=1)

    def action_toggle_subagents(self) -> None:
        self.show_subagents = not self.show_subagents
        self.refresh_sessions()
        self.notify(f"Subagents {'shown' if self.show_subagents else 'hidden'}", timeout=1)

    def action_pick_columns(self) -> None:
        def on_result(cols: list[str] | None) -> None:
            if cols is not None and cols:
                self._visible_cols = cols
                prefs = load_prefs()
                prefs["columns"] = cols
                save_prefs(prefs)
                self._rebuild_table_columns()
                self.refresh_sessions()
                self.notify("Columns updated", timeout=1)
        self.push_screen(ColumnPicker(self._visible_cols), on_result)

    def action_pause_all(self) -> None:
        working = [s for s in self.sessions if s.status == "working"]
        if not working:
            self.notify("No working sessions", timeout=1)
            return
        paused = 0
        try:
            result = subprocess.run(
                ["pgrep", "-f", "claude.*--dangerously-skip-permissions"],
                capture_output=True, text=True, timeout=2,
            )
            for line in result.stdout.strip().splitlines():
                try:
                    pid = int(line.strip())
                    os.kill(pid, signal.SIGINT)
                    paused += 1
                except (ValueError, ProcessLookupError, PermissionError):
                    continue
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        self.notify(f"Paused {paused} process(es)", timeout=2)

    def action_open_menu(self) -> None:
        table = self.query_one("#session-table", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self._flat_rows):
            return
        session = self._flat_rows[table.cursor_row]

        def handle_action(action: str | None) -> None:
            if action == "jump":
                focus_ghostty_session(session)
            elif action == "copy_id":
                copy_to_clipboard(session.session_id)
                self.notify("Copied", timeout=1)
            elif action == "remote" and session.remote_url:
                subprocess.run(["open", session.remote_url], capture_output=True)
            elif action == "transcript":
                subprocess.run(["open", "-R", session.transcript_path], capture_output=True)
        self.push_screen(SessionMenu(session), handle_action)

    def action_cursor_down(self) -> None:
        self.query_one("#session-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#session-table", DataTable).action_cursor_up()


def main():
    ClaudeMonitor().run()


if __name__ == "__main__":
    main()
