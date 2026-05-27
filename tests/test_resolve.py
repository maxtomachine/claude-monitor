"""Tests for the session identity resolver (the single chokepoint + self-audit).

Phase 1 foundation: resolve_session() is implemented and verified here but not yet
wired into build_session (that is the Phase 1b cutover). These tests pin the
precedence table and the invariant audit so the cutover can be proven
behavior-preserving and so each future violation becomes a fixture.
"""

from unittest.mock import patch

from claude_monitor import (
    INSTANCE_SEP,
    ResolvedIdentity,
    audit_identities,
    base_sid,
    resolve_session,
    _resolve_title,
)


def _data(custom_title="", cwd="/Users/test/proj", last_assistant_time=0.0):
    return {"custom_title": custom_title, "cwd": cwd,
            "last_assistant_time": last_assistant_time}


class TestBaseSid:
    def test_bare(self):
        assert base_sid("abc123") == "abc123"

    def test_legacy_at_pid(self):
        assert base_sid("abc123@4567") == "abc123"

    def test_new_instance_key(self):
        assert base_sid(f"abc123{INSTANCE_SEP}1779468421760") == "abc123"

    def test_idempotent(self):
        assert base_sid(base_sid("abc123@99")) == "abc123"


class TestResolveTitle:
    def test_live_custom_title_wins(self):
        hook = {"state": "idle", "title": "hook-name"}
        title, src = _resolve_title("sid", _data(custom_title="renamed"), {}, "/x", hook)
        assert (title, src) == ("renamed", "custom_title")

    def test_exited_prefers_settled_hook_over_straggler(self):
        hook = {"state": "exited", "title": "tools-frontier-curve"}
        title, src = _resolve_title("sid", _data(custom_title="tools-monitor"),
                                    {}, "/x", hook)
        assert (title, src) == ("tools-frontier-curve", "hook")

    def test_live_falls_to_hook_when_no_custom_title(self):
        hook = {"state": "idle", "title": "hook-name"}
        title, src = _resolve_title("sid", _data(custom_title=""), {}, "/x", hook)
        assert (title, src) == ("hook-name", "hook")

    def test_fallback_to_summary(self):
        title, src = _resolve_title("sid", _data(custom_title=""),
                                    {"summary": "from-index"}, "/x", None)
        assert (title, src) == ("from-index", "summary")

    def test_fallback_to_cwd_basename(self):
        with patch("claude_monitor.read_session_memory_title", return_value=""):
            title, src = _resolve_title("sid", _data(custom_title="", cwd="/a/myproj"),
                                        {}, "/x", None)
        assert (title, src) == ("myproj", "cwd")

    def test_last_resort_sid8(self):
        with patch("claude_monitor.read_session_memory_title", return_value=""):
            title, src = _resolve_title("abcdef1234", _data(custom_title="", cwd=""),
                                        {}, "/x", None)
        assert (title, src) == ("abcdef12", "sid")


class TestResolveSession:
    def _run(self, sid, *, alive, pid, started_ms, hook):
        with patch("claude_monitor.read_hook_state", return_value=hook), \
             patch("claude_monitor._is_session_alive", return_value=alive), \
             patch("claude_monitor._started_ms_for", return_value=started_ms), \
             patch("claude_monitor.determine_status", return_value="working"), \
             patch.dict("claude_monitor._pid_map", {sid: pid} if pid else {}, clear=True):
            return resolve_session(sid, _data(custom_title="t"), {}, "/x")

    def test_live_keyed_by_instance_id(self):
        r = self._run("sid", alive=True, pid=4567, started_ms=1779468421760,
                      hook={"state": "idle", "title": "t"})
        assert r.instance_id == f"sid{INSTANCE_SEP}1779468421760"
        assert r.key == r.instance_id
        assert r.origin == "live"
        assert r.source == "hook"
        assert r.alive is True

    def test_live_without_startedms_is_backfilled(self):
        r = self._run("sid", alive=True, pid=4567, started_ms=0,
                      hook={"state": "idle", "title": "t"})
        assert r.key == f"sid{INSTANCE_SEP}0"
        assert r.origin == "backfilled"

    def test_dead_with_pid_uses_legacy_grain(self):
        r = self._run("sid", alive=False, pid=4567, started_ms=0, hook=None)
        assert r.key == "sid@4567"
        assert r.origin == "reconstructed"
        assert r.source == "pidfile"

    def test_dead_nothing_keys_by_sid(self):
        r = self._run("sid", alive=False, pid=None, started_ms=0, hook=None)
        assert r.key == "sid"
        assert r.origin == "reconstructed"
        assert r.source == "transcript"

    def test_instance_id_stable_across_pid_recycle(self):
        # Same sid + same launch ms => same instance id even if pid differs.
        a = self._run("sid", alive=True, pid=111, started_ms=999,
                      hook={"state": "idle", "title": "t"})
        b = self._run("sid", alive=True, pid=222, started_ms=999,
                      hook={"state": "idle", "title": "t"})
        assert a.instance_id == b.instance_id


def _ident(key, sid, instance_id, alive=True, title_source="hook"):
    return ResolvedIdentity(
        key=key, sid=sid, instance_id=instance_id, pid=1, started_ms=1,
        title="t", title_source=title_source, status="working", alive=alive,
        cwd="/x", origin="live", source="hook")


class TestAuditIdentities:
    def test_clean_set_no_violations(self):
        ids = [_ident("a#1", "a", "a#1"), _ident("b#2", "b", "b#2")]
        assert audit_identities(ids) == []

    def test_dup_key_flagged(self):
        ids = [_ident("a#1", "a", "a#1"), _ident("a#1", "a", "a#1")]
        kinds = {v["kind"] for v in audit_identities(ids)}
        assert "dup_key" in kinds

    def test_key_sid_mismatch_flagged(self):
        ids = [_ident("a#1", "b", "b#1")]  # key normalizes to 'a', sid is 'b'
        kinds = {v["kind"] for v in audit_identities(ids)}
        assert "key_sid_mismatch" in kinds

    def test_dup_instance_id_flagged(self):
        ids = [_ident("a#1", "a", "shared#1"), _ident("a#2", "a", "shared#1")]
        kinds = {v["kind"] for v in audit_identities(ids)}
        assert "dup_instance_id" in kinds

    def test_empty_title_source_on_live_flagged(self):
        ids = [_ident("a#1", "a", "a#1", title_source="")]
        kinds = {v["kind"] for v in audit_identities(ids)}
        assert "title_source_unknown" in kinds

    def test_dead_row_empty_title_source_not_flagged(self):
        ids = [_ident("a", "a", "a#0", alive=False, title_source="")]
        assert audit_identities(ids) == []
