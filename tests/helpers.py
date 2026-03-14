"""Shared test helpers for claude-monitor tests."""

import json
import time

from claude_monitor import Session


def make_session(**overrides) -> Session:
    """Create a Session with sensible defaults, overridable via kwargs."""
    now = time.time()
    defaults = dict(
        session_id="abc12345-dead-beef-cafe-000000000001",
        project="test-project",
        title="Test Session",
        status="working",
        model="Opus 4.6",
        model_id="claude-opus-4-6",
        cost=1.50,
        tokens_in=50_000,
        tokens_out=10_000,
        context_pct=70,
        message_count=5,
        last_activity=now,
        created=now - 3600,
        cwd="/Users/test/Projects/myproject",
        transcript_path="/tmp/fake-transcript.jsonl",
        remote_url="https://claude.ai/code/session_abc123",
        slug="abc123",
    )
    defaults.update(overrides)
    return Session(**defaults)


def make_transcript_jsonl(
    messages: list[dict] | None = None,
    *,
    cwd: str = "/Users/test/project",
    model: str = "claude-opus-4-6",
    slug: str = "test-slug",
    custom_title: str = "",
    tokens_in: int = 1000,
    tokens_out: int = 500,
) -> str:
    """Build a minimal JSONL transcript string for testing scan_full_file."""
    lines = []

    if custom_title:
        lines.append(json.dumps({"type": "custom-title", "customTitle": custom_title}))

    lines.append(json.dumps({
        "type": "user",
        "cwd": cwd,
        "slug": slug,
        "timestamp": "2026-03-13T10:00:00Z",
    }))

    lines.append(json.dumps({
        "type": "assistant",
        "timestamp": "2026-03-13T10:00:05Z",
        "message": {
            "model": model,
            "usage": {
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "content": [
                {"type": "text", "text": "Here is my response."},
            ],
        },
    }))

    if messages:
        for msg in messages:
            lines.append(json.dumps(msg))

    return "\n".join(lines)
