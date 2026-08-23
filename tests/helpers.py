"""Shared test helpers for claude-monitor tests."""

import asyncio
import json
import time

from claude_monitor import Session


async def wait_for_refresh(pilot, *, since: int | None = None, timeout: float = 5.0) -> None:
    """Wait until the app has APPLIED a refresh newer than `since`, then let
    one more message pump run so widgets scheduled by that apply mount.

    Waits on the app's own _refresh_generation, which _refresh_apply bumps
    at its very end, so it cannot pass early and cannot pass without the
    UI reflecting the new state. `since` is the generation observed BEFORE
    triggering the refresh; omitted, it waits for the next completion after
    now. Fails loudly on timeout; a hang in refresh is a bug worth seeing."""
    app = pilot.app
    target = (app._refresh_generation if since is None else since) + 1
    deadline = time.monotonic() + timeout
    while app._refresh_generation < target:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"refresh did not apply within {timeout}s "
                f"(generation {app._refresh_generation}, wanted >= {target})")
        await asyncio.sleep(0.005)
    await pilot.pause()


async def settle(pilot, *, timeout: float = 5.0) -> None:
    """Let the app reach a quiescent state: every worker finished (refresh
    AND jump/resume/broadcast threads), every refresh that was requested
    has APPLIED, and one message pump has run after that.

    Replaces the old `await pilot.pause()` ritual. A fixed tick raced the
    refresh's main -> worker -> main hop and produced load-dependent false
    failures (two hand-bisected on 2026-08-18). Three things this must get
    right, each a hole a review found in the first cut (2026-08-19):

    1. Pump BEFORE looking: a keypress handler that refreshes runs from
       the message queue after pilot.press() returns, so the pending flag
       can still read False on entry.
    2. Wait for ALL workers, not just the refresh flags: a jump lands
       (focus + _mark_ready_seen) on a run_worker thread the refresh flags
       never mention, so asserting on `jumps` or persisted acks right
       after settle raced that thread.
    3. Prove the refresh APPLIED via _refresh_generation, not via the
       flags reading False: between the worker clearing _refresh_queued
       and the main thread running the re-scheduled refresh_sessions(),
       both flags are False with a refresh still to come.

    The loop runs until a full pass observes no live workers and no
    pending/queued refresh, with the generation unchanged across the pass,
    so a refresh that a worker schedules on its way out is still waited
    for. Bounded: a genuine hang fails in `timeout` seconds by name."""
    app = pilot.app
    deadline = time.monotonic() + timeout
    await pilot.pause()
    while True:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"app did not settle within {timeout}s "
                f"(pending={app._refresh_pending}, queued={app._refresh_queued}, "
                f"live workers={len([w for w in app.workers if w.is_running])})")
        gen_before = app._refresh_generation
        await app.workers.wait_for_complete()
        if app._refresh_pending or app._refresh_queued:
            await asyncio.sleep(0.005)
            continue
        await pilot.pause()
        # A worker finishing can schedule one more refresh via
        # call_from_thread; the pump above runs it, which flips pending
        # True again or bumps the generation. Either way, go around.
        if app._refresh_pending or app._refresh_queued or app._refresh_generation != gen_before:
            continue
        return


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
