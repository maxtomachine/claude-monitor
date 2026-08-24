"""Layout save/restore (Max, 2026-08-23: close Ghostty, reopen, get every
window and tab back where it was). The AX read and the Ghostty writes are
thin and untestable without a display; everything decision-bearing is in
the pure functions tested here: resolving AX tab titles to sessions,
building the restore plan, and the pin union on save."""

from unittest.mock import patch

import claude_monitor as cm
from claude_monitor import (
    _layout_from_snapshot,
    _restore_plan,
    save_layout,
    MONITOR_TAB_MARK,
)
from tests.helpers import make_session


def _raw(*windows):
    return list(windows)


def _win(frame, *tabs):
    return {"frame": list(frame), "tabs": [{"title": t, "active": a} for t, a in tabs]}


class TestLayoutFromSnapshot:
    def test_resolves_sid8_in_tab_title_to_full_sid(self):
        s = make_session(session_id="7a5651f1-bf71-45a2-a208-781f6bdf36ab", title="tools-monitor")
        raw = _raw(_win((1, 2, 3, 4), ("✳ tools-monitor ·7a5651f1", True)))
        layout = _layout_from_snapshot(raw, [s])
        tab = layout["windows"][0]["tabs"][0]
        assert tab["sid"] == s.session_id
        assert tab["monitor"] is False
        assert layout["windows"][0]["active"] == 0
        assert layout["windows"][0]["frame"] == [1, 2, 3, 4]

    def test_monitor_tab_is_marked_and_has_no_sid(self):
        raw = _raw(_win((0, 0, 0, 0), (f"◇ Claude Monitor {MONITOR_TAB_MARK}", True)))
        tab = _layout_from_snapshot(raw, [])["windows"][0]["tabs"][0]
        assert tab["monitor"] is True and tab["sid"] is None

    def test_tab_without_marker_is_kept_as_placeholder(self):
        """A plain zsh tab has no sid. It must stay in the list so the
        saved tab ORDER matches what Max sees."""
        s = make_session(session_id="aaaaaaaa-0000", title="x")
        raw = _raw(_win((0, 0, 0, 0), ("zsh", False), ("✳ x ·aaaaaaaa", True)))
        tabs = _layout_from_snapshot(raw, [s])["windows"][0]["tabs"]
        assert [t["sid"] for t in tabs] == [None, s.session_id]

    def test_unknown_sid8_resolves_to_none(self):
        raw = _raw(_win((0, 0, 0, 0), ("✳ gone ·deadbeef", True)))
        assert _layout_from_snapshot(raw, [])["windows"][0]["tabs"][0]["sid"] is None

    def test_sibling_pid_key_is_saved_as_the_bare_sid(self):
        """Review 2026-08-23: saving 'uuid@pid' pinned and restored a string
        that matches nothing once the pids are gone."""
        sib = make_session(session_id="cafecafe-0000@12345", title="c")
        raw = _raw(_win((0, 0, 0, 0), ("✳ c ·cafecafe", True)))
        assert _layout_from_snapshot(raw, [sib])["windows"][0]["tabs"][0]["sid"] == "cafecafe-0000"

    def test_subagents_never_match(self):
        sub = make_session(session_id="bbbbbbbb-0000", title="agent", is_subagent=True)
        raw = _raw(_win((0, 0, 0, 0), ("✳ agent ·bbbbbbbb", True)))
        assert _layout_from_snapshot(raw, [sub])["windows"][0]["tabs"][0]["sid"] is None

    def test_active_tab_index_preserved(self):
        a = make_session(session_id="aaaaaaaa-0000", title="a")
        b = make_session(session_id="bbbbbbbb-0000", title="b")
        raw = _raw(_win((0, 0, 0, 0), ("✳ a ·aaaaaaaa", False), ("✳ b ·bbbbbbbb", True)))
        assert _layout_from_snapshot(raw, [a, b])["windows"][0]["active"] == 1


class TestRestorePlan:
    def _layout(self, *windows):
        return {"windows": list(windows)}

    def _t(self, sid, monitor=False, title=""):
        return {"sid": sid, "monitor": monitor, "title": title}

    def test_builds_resume_commands_in_saved_tab_order(self, tmp_path):
        t1 = tmp_path / "a.jsonl"; t1.write_text("{}")
        t2 = tmp_path / "b.jsonl"; t2.write_text("{}")
        pa = tmp_path / "pa"; pa.mkdir()
        pb = tmp_path / "pb"; pb.mkdir()
        a = make_session(session_id="a", title="alpha", transcript_path=str(t1),
                         cwd=str(pa), project_path=str(pa))
        b = make_session(session_id="b", title="beta", transcript_path=str(t2),
                         cwd=str(pb), project_path=str(pb))
        layout = self._layout({"frame": [1, 2, 3, 4], "active": 1,
                               "tabs": [self._t("b"), self._t("a")]})
        plan, missing, skipped = _restore_plan(layout, [a, b])
        assert missing == [] and skipped == []
        assert [t["label"] for t in plan[0]["tabs"]] == ["beta", "alpha"]
        assert "claude --resume b" in plan[0]["tabs"][0]["cmd"]
        assert str(pb) in plan[0]["tabs"][0]["cmd"]
        assert plan[0]["active"] == 1
        assert plan[0]["frame"] == [1, 2, 3, 4]

    def test_monitor_tab_relaunches_the_monitor(self):
        layout = self._layout({"frame": [0, 0, 0, 0], "active": 0,
                               "tabs": [self._t(None, monitor=True)]})
        plan, _, _ = _restore_plan(layout, [])
        assert plan[0]["tabs"][0]["label"] == "claude-monitor"
        assert "claude-monitor" in plan[0]["tabs"][0]["cmd"]

    def test_monitor_tab_skipped_when_a_monitor_is_already_running(self):
        """Review 2026-08-23: a restore with the monitor still up spawned a
        second monitor instance."""
        layout = self._layout({"frame": [0, 0, 0, 0], "active": 0,
                               "tabs": [self._t(None, monitor=True)]})
        plan, _, _ = _restore_plan(layout, [], monitor_running=True)
        assert plan == []

    def test_plain_shell_placeholder_is_a_shell_first_and_empty_after(self):
        """A placeholder tab LEADING a window carries the title stamp and
        so must open a real shell; a placeholder later in the window opens
        Ghostty's default surface (empty command)."""
        layout = self._layout({"frame": [0, 0, 0, 0], "active": 0,
                               "tabs": [self._t(None, title="zsh"), self._t(None, title="zsh")]})
        plan, _, _ = _restore_plan(layout, [])
        assert "exec zsh" in plan[0]["tabs"][0]["cmd"]
        assert plan[0]["tabs"][1]["cmd"] == ""

    def test_missing_transcript_is_reported_and_skipped(self):
        gone = make_session(session_id="gone", title="gone", transcript_path="/nope/x.jsonl")
        layout = self._layout({"frame": [0, 0, 0, 0], "active": 0, "tabs": [self._t("gone")]})
        plan, missing, _ = _restore_plan(layout, [gone])
        assert missing == ["gone"]
        assert plan == []  # a window with no surviving tabs is not built

    def test_unknown_sid_is_reported_not_crashed(self):
        layout = self._layout({"frame": [0, 0, 0, 0], "active": 0, "tabs": [self._t("never-seen")]})
        plan, missing, _ = _restore_plan(layout, [])
        assert missing == ["never-seen"] and plan == []

    def test_live_session_is_skipped_never_duplicated(self, tmp_path):
        """Review 2026-08-23: every interactive resume path refuses to
        spawn a duplicate (Claude Code's single-instance guard kicks it,
        leaving a dead tab); restore must too. Covers both signals: the
        process is alive, or its sid8 is already in a terminal title."""
        t = tmp_path / "a.jsonl"; t.write_text("{}")
        a = make_session(session_id="aaaaaaaa-1111", title="a", transcript_path=str(t))
        b = make_session(session_id="bbbbbbbb-2222", title="b", transcript_path=str(t))
        layout = self._layout({"frame": [0, 0, 0, 0], "active": 0,
                               "tabs": [self._t("aaaaaaaa-1111"), self._t("bbbbbbbb-2222")]})
        plan, missing, skipped = _restore_plan(
            layout, [a, b], live_sids={"aaaaaaaa-1111"}, visible_sid8s={"bbbbbbbb"})
        assert missing == []
        assert sorted(skipped) == ["aaaaaaaa-1111", "bbbbbbbb-2222"]
        assert plan == []

    def test_active_tab_survives_a_dropped_tab_before_it(self, tmp_path):
        """Review 2026-08-23: saved tabs [A,B,C,D] active=2 (C); A is
        gone. Survivors [B,C,D]; a plain clamp min(2, 2) selected D. The
        saved index must follow the surviving tab that carried it."""
        t = tmp_path / "x.jsonl"; t.write_text("{}")
        mk = lambda sid: make_session(session_id=sid, title=sid, transcript_path=str(t))
        b, c, d = mk("b"), mk("c"), mk("d")
        layout = self._layout({"frame": [0, 0, 0, 0], "active": 2,
                               "tabs": [self._t("a"), self._t("b"), self._t("c"), self._t("d")]})
        plan, missing, _ = _restore_plan(layout, [b, c, d])
        assert missing == ["a"]
        assert [t["label"] for t in plan[0]["tabs"]] == ["b", "c", "d"]
        assert plan[0]["active"] == 1  # C

    def test_active_tab_clamps_when_the_active_tab_itself_is_dropped(self, tmp_path):
        t = tmp_path / "x.jsonl"; t.write_text("{}")
        a = make_session(session_id="a", title="a", transcript_path=str(t))
        layout = self._layout({"frame": [0, 0, 0, 0], "active": 1,
                               "tabs": [self._t("a"), self._t("gone")]})
        plan, missing, _ = _restore_plan(layout, [a])
        assert missing == ["gone"] and plan[0]["active"] == 0

    def test_sibling_pid_keys_resolve_to_the_bare_conversation(self, tmp_path):
        """Review 2026-08-23: a conversation open in two pids is listed as
        'uuid@pid' rows. A layout saved with that key, or a session list
        carrying it, must still resolve to the bare uuid."""
        t = tmp_path / "x.jsonl"; t.write_text("{}")
        sib = make_session(session_id="cafecafe-0000@12345", title="c", transcript_path=str(t))
        layout = self._layout({"frame": [0, 0, 0, 0], "active": 0,
                               "tabs": [self._t("cafecafe-0000@99999")]})
        plan, missing, _ = _restore_plan(layout, [sib])
        assert missing == []
        assert "claude --resume cafecafe-0000" in plan[0]["tabs"][0]["cmd"]
        assert "@" not in plan[0]["tabs"][0]["cmd"].split("--resume")[1].split("'")[0]


class TestSaveLayoutPins:
    def test_pins_every_claude_in_the_layout_and_keeps_existing_pins(self, tmp_path, monkeypatch):
        """The point of pinning on save: nothing ages out of the monitor
        while Ghostty is closed. Existing pins are kept; pinned_before is
        recorded so --restore-pins can put them back."""
        monkeypatch.setattr(cm, "PINNED_PATH", tmp_path / "pinned.json")
        monkeypatch.setattr(cm, "LAYOUT_PATH", tmp_path / "layout.json")
        cm.save_pinned_sessions({"already-pinned"})
        a = make_session(session_id="aaaaaaaa-0000", title="a")
        raw = [_win((0, 0, 0, 0), ("✳ a ·aaaaaaaa", True))]
        with patch("claude_monitor._snapshot_ghostty_layout", return_value=raw):
            layout = save_layout(sessions=[a])
        assert cm.load_pinned_sessions() == {"already-pinned", "aaaaaaaa-0000"}
        assert layout["pinned_before"] == ["already-pinned"]
        assert layout["summary"]["newly_pinned"] == 1
        assert layout["summary"]["claudes"] == 1
        assert cm.load_layout()["windows"][0]["tabs"][0]["sid"] == "aaaaaaaa-0000"

    def test_failed_snapshot_refuses_and_keeps_the_previous_layout(self, tmp_path, monkeypatch):
        """Review 2026-08-23: an Accessibility-denied shell, or a save after
        Ghostty quit, used to write `windows: []` over the last good
        snapshot and exit 0."""
        monkeypatch.setattr(cm, "PINNED_PATH", tmp_path / "pinned.json")
        monkeypatch.setattr(cm, "LAYOUT_PATH", tmp_path / "layout.json")
        cm.LAYOUT_PATH.write_text('{"windows": [{"frame": [1,1,1,1], "active": 0, "tabs": []}], "pinned_before": []}')
        for bad in (None, []):
            with patch("claude_monitor._snapshot_ghostty_layout", return_value=bad):
                result = save_layout(sessions=[])
            assert result["ok"] is False
            assert cm.load_layout()["windows"][0]["frame"] == [1, 1, 1, 1], bad
        assert cm.load_pinned_sessions() == set()

    def test_second_save_carries_the_original_pinned_before(self, tmp_path, monkeypatch):
        """Review 2026-08-23: re-reading the pin file on a second save
        recorded the first save's additions as "before", so --restore-pins
        could never roll back past the latest save."""
        monkeypatch.setattr(cm, "PINNED_PATH", tmp_path / "pinned.json")
        monkeypatch.setattr(cm, "LAYOUT_PATH", tmp_path / "layout.json")
        cm.save_pinned_sessions({"original"})
        a = make_session(session_id="aaaaaaaa-0000", title="a")
        raw = [_win((0, 0, 0, 0), ("✳ a ·aaaaaaaa", True))]
        with patch("claude_monitor._snapshot_ghostty_layout", return_value=raw):
            save_layout(sessions=[a])
            assert cm.load_pinned_sessions() == {"original", "aaaaaaaa-0000"}
            second = save_layout(sessions=[a])  # pin file now holds both
        assert second["pinned_before"] == ["original"]

    def test_save_uses_the_passed_session_list_not_a_fresh_parse(self, tmp_path, monkeypatch):
        """Review 2026-08-23: parsing on the Ctrl+L worker raced the refresh
        worker on the unlocked scan cache and could exit the monitor."""
        monkeypatch.setattr(cm, "PINNED_PATH", tmp_path / "pinned.json")
        monkeypatch.setattr(cm, "LAYOUT_PATH", tmp_path / "layout.json")
        a = make_session(session_id="aaaaaaaa-0000", title="a")
        raw = [_win((0, 0, 0, 0), ("✳ a ·aaaaaaaa", True))]
        with patch("claude_monitor.parse_sessions") as parse, \
             patch("claude_monitor._snapshot_ghostty_layout", return_value=raw):
            save_layout(sessions=[a])
            parse.assert_not_called()


class TestActiveTabDetection:
    def test_falls_back_to_window_title_when_radio_value_unreadable(self):
        """The first live save found every window at active tab 0 because
        the AX radio value compared as === 1 when it is a boolean. Beyond
        fixing the compare, the window title (which always names the
        selected tab) is a second witness."""
        a = make_session(session_id="aaaaaaaa-0000", title="a")
        b = make_session(session_id="bbbbbbbb-0000", title="b")
        raw = [{"name": "✳ b ·bbbbbbbb", "frame": [0, 0, 0, 0],
                "tabs": [{"title": "✳ a ·aaaaaaaa", "active": False},
                         {"title": "✳ b ·bbbbbbbb", "active": False}]}]
        assert _layout_from_snapshot(raw, [a, b])["windows"][0]["active"] == 1


class TestWindowStamp:
    """The restore plan stamps each window's first tab with a unique OSC
    title so _ghostty_build_window can find THAT window in the AX tree to
    frame it. Three schemes failed live before this one (2026-08-23):
    AX index 0 (was the monitor), a before/after name diff, and a
    before/after frame diff (fresh windows share one placeholder title and
    one default frame)."""

    def test_each_window_gets_a_distinct_stamp_on_its_first_tab(self):
        layout = {"windows": [
            {"frame": [0, 0, 0, 0], "active": 0, "tabs": [{"sid": None, "monitor": False, "title": "zsh"}]},
            {"frame": [0, 0, 0, 0], "active": 0, "tabs": [{"sid": None, "monitor": False, "title": "zsh"},
                                                           {"sid": None, "monitor": False, "title": "zsh"}]},
        ]}
        plan, _, _ = _restore_plan(layout, [])
        stamps = [w["stamp"] for w in plan]
        assert len(set(stamps)) == 2
        for w in plan:
            assert w["stamp"].startswith(cm.LAYOUT_STAMP_PREFIX)
            assert w["stamp"] in w["tabs"][0]["cmd"]          # first tab carries it
            for t in w["tabs"][1:]:
                assert w["stamp"] not in t["cmd"]             # later tabs do not

    def test_stamp_precedes_the_real_command(self, tmp_path):
        t = tmp_path / "a.jsonl"; t.write_text("{}")
        a = make_session(session_id="a", title="a", transcript_path=str(t), project_path=str(tmp_path))
        layout = {"windows": [{"frame": [0, 0, 0, 0], "active": 0,
                               "tabs": [{"sid": "a", "monitor": False}]}]}
        plan, _, _ = _restore_plan(layout, [a])
        cmd = plan[0]["tabs"][0]["cmd"]
        assert cmd.index("printf") < cmd.index("claude --resume a")
        assert "\\033]0;" in cmd and "\\007" in cmd  # OSC 0 title sequence

    def test_restamp_of_an_empty_placeholder_yields_a_shell(self):
        cmd = cm._restamp_surface_command("", "·layout-9-1")
        assert "·layout-9-1" in cmd and "exec zsh" in cmd


class TestTitleFallback:
    """Live, 2026-08-24, with stakes: a tab in Claude Nest voice mode is
    titled "◐ <name>" with no sid8 marker, so the session Max was
    actually talking to saved as a plain shell and would not have come
    back. Exact title match resolves it, only when the title names one
    conversation."""

    def test_markerless_tab_resolves_by_unique_title(self):
        s = make_session(session_id="7a5651f1-0000", title="tools-monitor")
        raw = [_win((0, 0, 0, 0), ("◐ tools-monitor", True))]
        assert _layout_from_snapshot(raw, [s])["windows"][0]["tabs"][0]["sid"] == "7a5651f1-0000"

    def test_sibling_rows_of_one_conversation_count_as_one(self):
        a1 = make_session(session_id="4b72a283-0000@111", title="prep-IHG")
        a2 = make_session(session_id="4b72a283-0000@222", title="prep-IHG")
        raw = [_win((0, 0, 0, 0), ("✳ prep-IHG", True))]
        assert _layout_from_snapshot(raw, [a1, a2])["windows"][0]["tabs"][0]["sid"] == "4b72a283-0000"

    def test_ambiguous_title_stays_unresolved(self):
        a = make_session(session_id="aaaaaaaa-0000", title="general")
        b = make_session(session_id="bbbbbbbb-0000", title="general")
        raw = [_win((0, 0, 0, 0), ("✳ general", True))]
        assert _layout_from_snapshot(raw, [a, b])["windows"][0]["tabs"][0]["sid"] is None

    def test_marker_wins_over_title(self):
        """A marker is authoritative; the title of a renamed tab may lag."""
        real = make_session(session_id="aaaaaaaa-0000", title="old-name")
        decoy = make_session(session_id="bbbbbbbb-0000", title="new-name")
        raw = [_win((0, 0, 0, 0), ("✳ new-name ·aaaaaaaa", True))]
        assert _layout_from_snapshot(raw, [real, decoy])["windows"][0]["tabs"][0]["sid"] == "aaaaaaaa-0000"


class TestSnapshotCompleteness:
    """2026-08-24, with stakes: the AX-only snapshot saw the current Space
    only and saved 4 of 41 windows, reporting success. The snapshot now
    comes from Ghostty's own dictionary (every window, every Space) joined
    to CoreGraphics frames; the pure layer must handle a window with no
    frame, and a save must keep the previous snapshot as .bak and report
    the window-count delta so a shrunken save is visible."""

    def test_window_without_frame_is_kept_and_not_framed(self):
        a = make_session(session_id="aaaaaaaa-0000", title="a")
        raw = [{"name": "✳ a ·aaaaaaaa", "frame": [0, 0, 0, 0],
                "tabs": [{"title": "✳ a ·aaaaaaaa", "active": True}]}]
        layout = _layout_from_snapshot(raw, [a])
        assert layout["windows"][0]["frame"] == [0, 0, 0, 0]
        assert layout["windows"][0]["tabs"][0]["sid"] == "aaaaaaaa-0000"

    def test_save_keeps_previous_snapshot_as_bak_and_reports_delta(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cm, "PINNED_PATH", tmp_path / "pinned.json")
        monkeypatch.setattr(cm, "LAYOUT_PATH", tmp_path / "layout.json")
        a = make_session(session_id="aaaaaaaa-0000", title="a")
        ten = [_win((0, 0, 1, 1), ("✳ a ·aaaaaaaa", True))] * 10
        four = [_win((0, 0, 1, 1), ("✳ a ·aaaaaaaa", True))] * 4
        with patch("claude_monitor._snapshot_ghostty_layout", return_value=ten):
            first = save_layout(sessions=[a])
        assert first["summary"]["previous_windows"] is None
        with patch("claude_monitor._snapshot_ghostty_layout", return_value=four):
            second = save_layout(sessions=[a])
        assert second["summary"]["previous_windows"] == 10
        assert second["summary"]["windows"] == 4
        import json
        bak = json.loads((tmp_path / "layout.json.bak").read_text())
        assert len(bak["windows"]) == 10


class TestCompactRestore:
    """2026-08-24: a literal rebuild of a 41-window layout put all 41 on
    one Space (macOS cannot place a window elsewhere) with 44 Claude TUIs
    at once. Unusable, and Ghostty went down minutes later taking every
    session with it. Restore now folds one-tab windows into a few grouped,
    tabbed windows by default; --exact keeps the literal rebuild."""

    def _single(self, sid, title):
        return {"frame": [0, 0, 100, 100], "active": 0,
                "tabs": [{"sid": sid, "monitor": False, "title": title}]}

    def _sessions(self, tmp_path, *titles):
        t = tmp_path / "x.jsonl"; t.write_text("{}")
        return [make_session(session_id=f"sid-{i}", title=n, transcript_path=str(t))
                for i, n in enumerate(titles)]

    def test_single_tab_windows_fold_into_grouped_windows(self, tmp_path):
        names = [f"frontier-{i}" for i in range(5)] + [f"strategy-{i}" for i in range(3)]
        sessions = self._sessions(tmp_path, *names)
        layout = {"windows": [self._single(s.session_id, s.title) for s in sessions]}
        plan, _, _ = _restore_plan(layout, sessions, compact=True)
        assert len(plan) == 2, [w.get("group") for w in plan]
        assert {w["group"] for w in plan} == {"frontier", "strategy"}
        assert sum(len(w["tabs"]) for w in plan) == 8

    def test_exact_mode_keeps_one_window_per_saved_window(self, tmp_path):
        sessions = self._sessions(tmp_path, "a-1", "a-2", "a-3")
        layout = {"windows": [self._single(s.session_id, s.title) for s in sessions]}
        plan, _, _ = _restore_plan(layout, sessions, compact=False)
        assert len(plan) == 3
        assert all(w["frame"] == [0, 0, 100, 100] for w in plan)

    def test_multi_tab_windows_are_kept_exactly_with_their_frame(self, tmp_path):
        sessions = self._sessions(tmp_path, "a-1", "a-2", "solo-9")
        layout = {"windows": [
            {"frame": [7, 8, 9, 10], "active": 1,
             "tabs": [{"sid": "sid-0", "monitor": False}, {"sid": "sid-1", "monitor": False}]},
            self._single("sid-2", "solo-9"),
        ]}
        plan, _, _ = _restore_plan(layout, sessions, compact=True)
        kept = [w for w in plan if w["frame"] == [7, 8, 9, 10]]
        assert len(kept) == 1 and len(kept[0]["tabs"]) == 2 and kept[0]["active"] == 1

    def test_groups_of_one_pool_into_misc_instead_of_a_window_each(self, tmp_path):
        sessions = self._sessions(tmp_path, "alpha-x", "beta-y", "gamma-z")
        layout = {"windows": [self._single(s.session_id, s.title) for s in sessions]}
        plan, _, _ = _restore_plan(layout, sessions, compact=True)
        assert len(plan) == 1 and plan[0]["group"] == "misc"
        assert len(plan[0]["tabs"]) == 3

    def test_a_group_larger_than_the_cap_splits_into_several_windows(self, tmp_path):
        names = [f"frontier-{i}" for i in range(19)]
        sessions = self._sessions(tmp_path, *names)
        layout = {"windows": [self._single(s.session_id, s.title) for s in sessions]}
        plan, _, _ = _restore_plan(layout, sessions, compact=True)
        assert len(plan) == 3                      # 8 + 8 + 3
        assert max(len(w["tabs"]) for w in plan) == 8

    def test_every_window_gets_its_own_stamp_after_compaction(self, tmp_path):
        names = [f"frontier-{i}" for i in range(3)] + [f"strategy-{i}" for i in range(3)]
        sessions = self._sessions(tmp_path, *names)
        layout = {"windows": [self._single(s.session_id, s.title) for s in sessions]}
        plan, _, _ = _restore_plan(layout, sessions, compact=True)
        stamps = [w["stamp"] for w in plan]
        assert len(set(stamps)) == len(plan)
        for w in plan:
            assert w["stamp"] in w["tabs"][0]["cmd"]

    def test_the_same_session_saved_twice_gets_one_tab(self, tmp_path):
        """A layout can hold one sid in two windows (same session open in
        two terminals). Resuming it twice just makes Claude's
        single-instance guard kill one."""
        sessions = self._sessions(tmp_path, "dup")
        sid = sessions[0].session_id
        layout = {"windows": [self._single(sid, "dup"), self._single(sid, "dup")]}
        plan, _, _ = _restore_plan(layout, sessions, compact=True)
        assert sum(len(w["tabs"]) for w in plan) == 1


class TestJumpbackNeverSilentlyNoOps:
    """2026-08-24: during the 41-window restore, Ctrl+Shift+Space did
    nothing at all (jumpback logged err:proc_live_no_tab five times).
    Its walk came back empty because Ghostty was too busy to answer, and
    the anti-duplicate guard read that as "monitor is absent but a process
    is alive, so refuse" precisely when Max most needed the key. The walk
    now reports how many windows it examined, so a mute Ghostty is told
    apart from real absence."""

    def _run(self, walk_output, ghostty_running=True, monitor_ttys=()):
        import subprocess, textwrap
        script = textwrap.dedent(f'''
            walk() {{ echo "{walk_output}"; }}
            pgrep() {{ case "$*" in *ghostty*) return {0 if ghostty_running else 1};; esac; return 1; }}
            result=""
            for attempt in 1 2 3; do
                result="$(walk)"
                case "$result" in
                    ok:*) break ;;
                    none:0:*) pgrep -qx ghostty || break ;;
                    err:*|"") : ;;
                    *) break ;;
                esac
            done
            case "$result" in
                none:0:*)
                    if pgrep -qx ghostty; then result="err:ghostty_mute"; else result="not_found"; fi
                    ;;
                none:*) result="not_found${{result#none}}" ;;
            esac
            case "$result" in not_found*) result="LAUNCH" ;; esac
            echo "$result"
        ''')
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip()

    def test_found_tab_raises_it(self):
        assert self._run("ok:tab") == "ok:tab"

    def test_mute_ghostty_is_never_read_as_absence(self):
        """The exact failure: Ghostty up but answering with zero windows."""
        assert self._run("none:0:0", ghostty_running=True) == "err:ghostty_mute"

    def test_ghostty_actually_gone_launches_a_monitor(self):
        assert self._run("none:0:0", ghostty_running=False) == "LAUNCH"

    def test_reliable_absence_launches_a_monitor(self):
        """Walk examined real windows and found no monitor tab: launch,
        rather than refusing because some process is alive."""
        assert self._run("none:12:37") == "LAUNCH"

    def test_walk_error_is_retried_not_treated_as_absence(self):
        assert self._run("err:walk:boom").startswith("err:")
