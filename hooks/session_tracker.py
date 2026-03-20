#!/usr/bin/env python3
"""
Claude Code session state tracker (hook script).

Called by Claude Code hooks on each event. Writes JSON state files to
~/.claude/session-states/ for the monitor to read.

States: thinking, idle, approval, exited
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOCAL_STATE_DIR = Path.home() / ".claude" / "session-states"


def find_claude_pid_and_tty(session_id: str) -> tuple[int, str]:
    """Find the Claude process PID and TTY.

    Walks up from the hook script until it finds a process whose comm is
    'claude'. Falls back to searching ~/.claude/sessions/ PID files.
    Returns (pid, tty) or (0, "") on failure.
    """
    # Walk up from ourselves
    pid = os.getpid()
    for _ in range(8):
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,tty=,comm="],
                capture_output=True, text=True, timeout=1,
            )
            parts = result.stdout.strip().split(None, 2)
            if len(parts) < 3:
                break
            ppid, tty, comm = int(parts[0]), parts[1], parts[2]
            if "claude" in comm.lower() and not comm.endswith(".app"):
                return pid, tty
            pid = ppid
        except (ValueError, subprocess.TimeoutExpired, OSError):
            break

    # Fallback: search PID files
    sessions_dir = Path.home() / ".claude" / "sessions"
    if sessions_dir.exists():
        for f in sessions_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("sessionId") == session_id:
                    pid = int(data["pid"])
                    result = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "tty="],
                        capture_output=True, text=True, timeout=1,
                    )
                    tty = result.stdout.strip()
                    return pid, tty
            except (json.JSONDecodeError, OSError, KeyError, ValueError,
                    subprocess.TimeoutExpired):
                continue
    return 0, ""


STATE_EMOJI = {
    "thinking": "⠐",
    "idle": "✳",
    "approval": "◉",
    "exited": "⊘",
}


def set_terminal_title(tty: str, state: str, session_id: str, name: str) -> None:
    """Write title escape sequence to the TTY with a unique session marker."""
    if not tty or tty == "??":
        return
    emoji = STATE_EMOJI.get(state, "✳")
    sid8 = session_id[:8]
    if len(name) > 32:
        name = name[:31] + "…"
    title = f"{emoji} {name} ·{sid8}"
    try:
        with open(f"/dev/{tty}", "w") as f:
            f.write(f"\x1b]2;{title}\x07")
    except OSError:
        pass


def ensure_state_dir() -> None:
    LOCAL_STATE_DIR.mkdir(parents=True, exist_ok=True)


def get_local_state_file(session_id: str) -> Path:
    return LOCAL_STATE_DIR / f"{session_id}.json"


def read_session_memory_title(transcript_path: str | None, session_id: str | None = None) -> str:
    """Read session title from session-memory/summary.md next to the transcript."""
    candidates = []

    if transcript_path:
        base = transcript_path
        if base.endswith(".jsonl"):
            base = base[:-6]
        candidates.append(Path(base) / "session-memory" / "summary.md")

    if session_id:
        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.exists():
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                candidate = project_dir / session_id / "session-memory" / "summary.md"
                if candidate not in candidates:
                    candidates.append(candidate)

    for summary_path in candidates:
        try:
            if not summary_path.exists():
                continue
            text = summary_path.read_text()
            in_title_section = False
            for line in text.splitlines():
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
            continue

    return ""


def find_transcript_path(session_id: str) -> str | None:
    """Find transcript JSONL for a session by searching project dirs."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return str(candidate)
    return None


def get_api_key() -> str | None:
    """Find an Anthropic API key from standard locations."""
    key = os.environ.get("ANTHROPIC_API_KEY")
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
    return None


def generate_title_background(transcript_path: str | None, state_file_path: Path) -> None:
    """Spawn a background process to generate a title via Haiku API."""
    api_key = get_api_key()
    if not api_key:
        return

    script = f'''
import json, sys
from pathlib import Path

state_file = Path({str(state_file_path)!r})
transcript_path = {transcript_path!r}

user_messages = []
if transcript_path:
    try:
        tp = Path(transcript_path)
        if tp.exists():
            for line in tp.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "user":
                    msg = entry.get("message", {{}})
                    content = msg.get("content", "") if isinstance(msg, dict) else ""
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                content = block.get("text", "")
                                break
                            elif isinstance(block, str):
                                content = block
                                break
                    if isinstance(content, str) and content.strip():
                        user_messages.append(content.strip()[:200])
    except Exception:
        pass

if not user_messages:
    sys.exit(0)

messages_text = "\\n---\\n".join(user_messages[-5:])

try:
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
        messages=[{{"role": "user", "content": f"In 5-10 words, what is this Claude Code session for? Just the title, no quotes or punctuation at the end.\\n\\nUser messages:\\n{{messages_text}}"}}],
    )
    title = resp.content[0].text.strip()
except Exception:
    sys.exit(0)

if not title:
    sys.exit(0)

try:
    from datetime import datetime
    data = json.loads(state_file.read_text())
    data["title"] = title
    data["title_source"] = "haiku"
    data["title_updated_at"] = datetime.now().isoformat()
    state_file.write_text(json.dumps(data, indent=2) + "\\n")
except Exception:
    pass
'''
    env = os.environ.copy()
    env["ANTHROPIC_API_KEY"] = api_key
    try:
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except OSError:
        pass


def write_state(
    session_id: str,
    state: str,
    cwd: str | None = None,
    transcript_path: str | None = None,
    extra: dict | None = None,
    event: str | None = None,
) -> None:
    """Write session state to local JSON file."""
    ensure_state_dir()
    local_file = get_local_state_file(session_id)

    existing_title = ""
    existing_title_source = ""
    existing_title_updated_at = ""
    existing_started_at = ""
    existing_state = ""
    existing_state_entered_at = ""
    existing_pid = 0
    existing_tty = ""
    if local_file.exists():
        try:
            existing = json.loads(local_file.read_text())
            existing_title = existing.get("title", "")
            existing_title_source = existing.get("title_source", "")
            existing_title_updated_at = existing.get("title_updated_at", "")
            existing_started_at = existing.get("started_at", "")
            existing_state = existing.get("state", "")
            existing_state_entered_at = existing.get("state_entered_at", "")
            existing_pid = existing.get("pid", 0)
            existing_tty = existing.get("tty", "")
        except (json.JSONDecodeError, OSError):
            pass

    title = existing_title
    title_source = existing_title_source
    title_updated_at = existing_title_updated_at
    started_at = existing_started_at or datetime.now().isoformat()

    if event == "stop":
        resolved_transcript = transcript_path or find_transcript_path(session_id)

        sm_title = read_session_memory_title(resolved_transcript, session_id)
        if sm_title and sm_title != existing_title:
            title = sm_title
            title_source = "session-memory"
            title_updated_at = datetime.now().isoformat()

        if not title and existing_title_source != "haiku":
            try:
                started = datetime.fromisoformat(started_at)
                elapsed = (datetime.now() - started).total_seconds()
                if elapsed >= 120:
                    generate_title_background(resolved_transcript, local_file)
            except (ValueError, TypeError):
                pass

    if existing_pid and existing_tty:
        pid, tty = existing_pid, existing_tty
    else:
        pid, tty = find_claude_pid_and_tty(session_id)
        pid = pid or existing_pid
        tty = tty or existing_tty

    now = datetime.now().isoformat()
    if state != existing_state or not existing_state_entered_at:
        state_entered_at = now
    else:
        state_entered_at = existing_state_entered_at

    # Terminal title: best known name, falling back to cwd basename
    display_name = title or (Path(cwd).name if cwd else "") or "Claude"

    data = {
        "session_id": session_id,
        "state": state,
        "state_entered_at": state_entered_at,
        "timestamp": now,
        "cwd": cwd,
        "pid": pid,
        "tty": tty,
        "started_at": started_at,
    }
    if title:
        data["title"] = title
        data["title_source"] = title_source
        data["title_updated_at"] = title_updated_at
    if extra:
        data.update(extra)

    local_file.write_text(json.dumps(data, indent=2) + "\n")

    # Set terminal title with unique session marker for jump-to-terminal
    set_terminal_title(tty, state, session_id, display_name)


def mark_exited(session_id: str) -> None:
    """Mark session as exited — terminal state."""
    local_file = get_local_state_file(session_id)
    if local_file.exists():
        try:
            data = json.loads(local_file.read_text())
            data["state"] = "exited"
            data["exited_at"] = datetime.now().isoformat()
            data["timestamp"] = datetime.now().isoformat()
            local_file.write_text(json.dumps(data, indent=2) + "\n")
        except (json.JSONDecodeError, OSError):
            pass


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: session_tracker.py <event>", file=sys.stderr)
        sys.exit(1)

    event = sys.argv[1]

    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        hook_input = {}

    session_id = hook_input.get("session_id", "unknown")
    cwd = hook_input.get("cwd")
    transcript_path = hook_input.get("transcript_path")

    # Guard: never overwrite "exited" — session_end is terminal
    local_file = get_local_state_file(session_id)
    if local_file.exists() and event not in ("session_start", "session_end"):
        try:
            current = json.loads(local_file.read_text())
            if current.get("state") == "exited":
                return
        except (json.JSONDecodeError, OSError):
            pass

    if event == "session_start":
        write_state(session_id, "idle", cwd, transcript_path, event=event)
    elif event == "user_prompt_submit":
        write_state(session_id, "thinking", cwd, transcript_path)
    elif event in ("pre_tool_use", "post_tool_use"):
        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input", {}) if isinstance(hook_input.get("tool_input"), dict) else {}
        file_path = tool_input.get("file_path", "")
        # Ignore session-memory edits
        if "session-memory" in file_path:
            return
        # Don't flip idle→thinking — only user_prompt_submit does that.
        # Subagent tool calls fire pre_tool_use with parent session ID after stop.
        if local_file.exists():
            try:
                current = json.loads(local_file.read_text())
                if current.get("state") == "idle":
                    return
            except (json.JSONDecodeError, OSError):
                pass
        extra = {"tool": tool_name}
        if file_path:
            extra["tool_target"] = file_path
        elif tool_input.get("command"):
            extra["tool_target"] = str(tool_input["command"])[:80]
        elif tool_input.get("pattern"):
            extra["tool_target"] = str(tool_input["pattern"])[:60]
        elif tool_input.get("url"):
            extra["tool_target"] = str(tool_input["url"])[:80]
        write_state(
            session_id, "thinking", cwd, transcript_path, extra
        )
    elif event == "permission_request":
        write_state(
            session_id, "approval", cwd, transcript_path,
            {"tool": hook_input.get("tool_name")}
        )
    elif event == "stop":
        write_state(session_id, "idle", cwd, transcript_path, event=event)
    elif event == "session_end":
        mark_exited(session_id)


if __name__ == "__main__":
    main()
