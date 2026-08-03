
# claude-monitor UI rules (migrated from auto-memory 2026-05-25)

## substring_match_home_basename
---
name: Never use home-dir basename as substring match candidate
description: Path.home().name (= username) is dangerously broad for window-title substring matching — it matches any window containing the username, causing wrong-target jumps
type: feedback
---

When building a fallback chain of candidates for window-title substring matching, never include the home directory basename (`Path.home().name`). It's the user's login name and will match any window title containing that string — including other sessions' hook-set titles that embed the username.

**Why:** claude-monitor jump log analysis (2026-03-27): session `63345fae` had no `·sid8` marker (Claude clobbered it post-Stop). The candidate chain fell through to cwd basename `mk` (from `/Users/mk`), which substring-matched `✳ mk ·6cb05ee4` — a completely different session's window. User jumped to the wrong terminal twice before reporting it. The next three attempts returned `no_match` because the wrong-window focus had changed which windows were visible.

**How to apply:** In any title/name matching fallback chain:
- Check `if candidate == Path.home().name: skip` before adding cwd-derived candidates
- More broadly: any candidate shorter than ~4 chars or equal to common identifiers (username, hostname, "main", "src") should be rejected — the false-positive rate outweighs the catch rate
- Prefer failing to match (triggering a clean fallback like resume-in-new-tab) over matching the wrong target

## fallback_to_resume
---
name: Fallback to resume instead of failing — reconnect to orphaned sessions
description: When a monitor action (jump, rename, send command) can't find the target terminal window, don't just toast an error — fall back to opening a new terminal and resuming the session there
type: feedback
---

When an action that targets a specific terminal window fails to find it, fall back to opening a new tab and resuming the session rather than reporting failure.

**Why:** User asked for this when the jump-to-terminal and rename actions showed "Could not find terminal" for sessions whose windows existed but couldn't be matched. The session is still running — the user wants to interact with it, not be told it's unreachable. Resume reconnects to the existing session (not a duplicate), and the hooks fire immediately to set up tracking on the new tab.

**How to apply:** Any monitor action that sends commands to a terminal (jump, rename, debrief, custom commands) should follow the pattern: try `_send_to_terminal_session()` first, fall back to `resume_session()` on failure. The resume opens a Ghostty tab (cmd+T via JXA), types `cd {cwd} && claude --resume {sid}`, and returns. This is a general pattern for resilient session interaction — don't dead-end on window-matching failures.

## textual_theme_tokens
---
name: Textual CSS — use theme tokens not hardcoded rgba for modal backgrounds
description: Hardcoded rgba(0,0,0,X) in Textual modal CSS breaks light mode; use $background/$panel/$boost which auto-invert
type: feedback
---

When writing Textual ModalScreen CSS, use theme tokens instead of hardcoded rgba colors:
- `$background` — page background
- `$panel` — card/container backgrounds
- `$boost` — highlight/selected state
- `$primary-darken-2` (or `-lighten-2`) — borders that adapt direction

NOT `rgba(0, 0, 0, 0.7)` or `$surface-darken-3` — these don't invert in light mode.

**Why:** User said "light mode doesn't carry over to kanban view" after adding a theme toggle (2026-03-19). The modal's `background: rgba(0, 0, 0, 0.7)` was hardcoded black; `$surface-darken-2` and `$surface-lighten-1` kept their dark-mode direction. Swapping to `$background`, `$panel`, `$boost` fixed it — these tokens are defined per-theme and flip correctly.

**How to apply:** Any new ModalScreen or overlay in a Textual app that supports theme switching must use the semantic color tokens. If you need a translucent backdrop, use `$background 70%` not `rgba(0,0,0,0.7)`. Check all modals when adding a theme toggle — SessionMenu, ColumnPicker, etc. likely have the same hardcoded pattern copied.


## Relocated from auto-memory 2026-07-16: feedback_config_defaults_at_spawn.md

---
name: settings.json is the live toggle state for /fast — always read it, ignore output_style
description: fastMode in settings.json is updated live by /fast toggle; output_style.name tracks rendering style NOT fast-mode; always read settings.json directly for fast-mode state
type: feedback
---

`settings.json:fastMode` is the LIVE source of truth for fast mode — updated every time the user runs `/fast`. Always read it directly. Do NOT use `output_style.name` from the status JSON — it tracks rendering style, not the `/fast` toggle.

**Why:** Four rounds of bugs (2026-03-20): (1) Only checked `output_style.name == "fast"` — rarely set, so fast indicator never showed. (2) Added settings.json as fallback for when output_style was empty — but output_style is always `"default"` (never empty), which was misread as "fast is off". (3) Fixed fallback to only apply when output_style truly absent — but it's never absent, so settings was never consulted. (4) Final fix: read `settings.json:fastMode` directly, always, ignore output_style entirely.

**How to apply:** For any status indicator backed by a settings.json toggle:
- Read `settings.json` directly — it IS the toggle state, not a default
- Don't use status JSON fields with similar names that track different things
- Don't initialize variables to false then check them later — the initialization overwrites the config read

The same applies to `effortLevel` in settings.json — it's the configured default but can be empty (user may remove it). Effort resolution: transcript-parsed last `/effort` command → settings.json `effortLevel` fallback.


## Relocated from auto-memory 2026-07-16: feedback_dual_mode_grouping_scheme.md

---
name: Dual-mode grouping — prefix-implicit AND @-suffix-explicit in one scheme
description: When building a grouping feature, support both implicit first-word prefix grouping AND explicit @-suffix grouping so users can force a group when prefixes differ, without the two modes clashing
type: feedback
---

For session/item grouping, combine two extraction modes in one `_group_key()` function: if the name contains `@`, the part AFTER `@` is the explicit group; otherwise the first word before any separator (`[\s\-/_.:]+`) is the implicit group. This lets users force a group when prefixes differ, without needing a separate configuration layer.

**Why:** claude-monitor grouping (2026-03-27). User: "If the name of the claude has the same first word before a space or a - or a / etc. they can be grouped... and you can force groups with the '@' sign in the name (think emails like bugs@disclosey groups with ideation@disclosey). This way we can support prefix or suffix grouping without clashes." The insight: prefix grouping is free (most names already have natural prefixes like `strategy-foo`, `strategy-bar`), but it fails when related items have unrelated prefixes (`bugs-disclosey` vs `ideation-disclosey` — both "disclosey" work but prefixes differ). The `@` is a cheap escape hatch that coexists with the prefix mode instead of replacing it.

**How to apply:**
- `@` takes precedence: check for `@` first, use `rsplit('@', 1)[1]` as the key
- Otherwise regex-split on separators and take `parts[0]`
- Both paths feed the same `_group_key()` — callers don't care which mode fired
- This pattern generalizes: any time you have an implicit heuristic that covers 80%, add an explicit-marker escape hatch (sigil, prefix, flag) for the 20% rather than adding config


## Relocated from auto-memory 2026-07-16: feedback_onboarding_countdown_tooltip.md

---
name: Onboarding via first-N-launches countdown tooltip
description: Teach a keybind or hidden feature by showing a toast on the first N launches with a visible countdown — self-dismissing, persists across restarts, tells users it will stop nagging
type: feedback
---

For teaching a keybind or non-discoverable feature in a distributed tool, show a toast on the first N launches with a visible countdown. The countdown serves two purposes: it teaches, AND it promises the nagging will end.

**Why:** claude-monitor jumpback (2026-03-27). The Ctrl+Shift+Space hotkey is invisible — there's no button, no menu item, it's a global skhd binding. User: "when a user uses claude monitor the first 20 times they should see in the statusline a tooltip to teach them about the hotkey." The countdown in the copy ("19 more reminders") is the key detail — it tells users this is onboarding, not a permanent annoyance, so they don't hunt for a "dismiss forever" option.

**How to apply:**
```python
prefs = load_prefs()
launches = prefs.get("launch_count", 0) + 1
prefs["launch_count"] = launches
save_prefs(prefs)
if launches <= 20:
    self.notify(
        f"Press Ctrl+Shift+Space from any app to return here "
        f"[dim]({21 - launches} more reminders)[/]",
        title="jumpback", timeout=6,
    )
```
- Persist the counter in the same prefs file as other settings — it survives restarts and `R` hot-reloads
- 20 is a good default for a single keybind; scale down for simpler features, up for multi-step workflows
- The dimmed countdown suffix is the anti-annoyance signal — don't skip it
- Mention it in the README ("the monitor will remind you about the hotkey for your first 20 launches") so users who read docs know what to expect


## Relocated from auto-memory 2026-07-16: feedback_session_delta_not_cumulative.md

---
name: Show per-session delta, not cumulative totals
description: When an external API returns a cumulative metric (account-lifetime credits, all-time token count), snapshot at session start and display delta — cumulative reads as misleading "this session cost X"
type: feedback
---

When displaying a metric fetched from an external/account-level API, if the metric is cumulative (all-time, account-wide), snapshot it at session start and show the delta — not the raw value.

**Why:** claude-monitor statusline showed `+$2759` from `extra_usage.used_credits` (cumulative cents, all time). User immediately caught it: "are we certain the math is right here for cost? it's 2K for THIS session???" The number was technically correct but contextually wrong — a per-session statusline displaying an account-lifetime total reads as "this session cost $2759" which is absurd and erodes trust in every other number on the line.

**How to apply:** For any metric that can only go up and is scoped wider than the current unit of display (session, run, request), stash the first value seen and show `now - snap`. Also check the unit — this case was cents-as-dollars too. Both errors compound: cumulative × wrong-unit = 100,000× off.

**Snapshot storage must survive reboot (2026-03-24):** Do NOT store the snapshot in `/tmp` — macOS clears it on reboot. When /tmp cleared, every running session re-snapshot at the same moment with the same account-wide value, so all statuslines showed an identical delta ("$4k in every line"). Store alongside persistent session state (`~/.claude/session-states/{sid}.extra-snap`) so the baseline survives reboots and each session's delta stays independent.


## Relocated from auto-memory 2026-07-16: feedback_stacked_modals.md

---
name: Stack modals on top instead of dismiss-and-reopen
description: When a secondary view (kanban, detail panel) opens an action menu, push the menu on top so the user stays in-context; escape goes back to the view, not the primary table
type: feedback
---

Secondary views that offer actions should push the action menu ON TOP of themselves, not dismiss back to the primary view first.

**Why:** User said "can we make the actions menu appear as-is on the kanban view instead of going back to the rows view with it open." The dismiss-and-reopen flow breaks context — after acting, the user lands somewhere different from where they chose to act. Stacking preserves spatial context: act → close menu → still on the board, same card selected.

**How to apply:** When building a modal screen that offers per-item actions (kanban card → menu, detail view → menu), have the modal push the action menu via `self.app.push_screen(ActionMenu(...), handler)` rather than `self.dismiss(item_id)` with a parent-side callback that reopens. In Textual this means the ModalScreen holds a reference to the handler (or fetches it via `self.app._make_handler`) and pushes directly.


## Relocated from auto-memory 2026-07-16: feedback_statusline_clear_eol.md

---
name: Statusline rows must end with \033[K to clear stale content
description: Custom statusline scripts must append ANSI clear-to-end-of-line before each \n — otherwise growing input area leaves prompt fragments visible past the statusline's own width
type: feedback
---

Every line a statusline script outputs should end with `\033[K` (ANSI erase-in-line, clears from cursor to end of line) before the `\n`. Without it, when the input area grows and pushes old content into the statusline's rows, the next statusline refresh only overwrites up to its own width — leaving fragments of the user's prompt visible to the right.

**Why:** claude-monitor statusline bug (2026-03-24): user typed a long multi-line prompt and saw their text bleeding through after `ctx ... 110k tok` — e.g. `110k tokFinally, let's do a`. The statusline was correct; it just didn't clear the rest of the row, so pushed-down prompt text remained visible where the statusline ended.

**How to apply:** In any bash statusline script: `printf '%b\033[K\n' "$line"` instead of `printf '%b\n' "$line"`. Apply to ALL output lines including empty ones (`printf '\033[K\n'`). This is distinct from the TTY-write corruption issue (escape sequences interleaving with keystrokes) — this one is about the statusline's own rendering not clearing its allocated rows fully.


## Relocated from auto-memory 2026-07-16: project_claude_monitor_hooks.md

---
name: claude-monitor hook system and TUI features
description: Hook-based state tracking + kanban + jump + grouping + gruvbox + BUSY status + history-hide + view-persist; PRs #6-11 all merged to main 2026-05-11; catchmeup fixed; Python pinned 3.12 (Santa)
type: project
originSessionId: 7a5651f1-bf71-45a2-a208-781f6bdf36ab
---
claude-monitor at `~/Projects/claude-monitor` (GitHub: maxtomachine/claude-monitor). Alpha distributed 2026-03-20 to group DM C0AMVP9MCGL (krk, dongjin, jamal, giodem, gautam, royarsan, jcham). Stable — survived full day of multi-session use (2026-03-24). Full-codebase sweep 2026-03-27: 17 bugs (6 high including a shell injection via Python `!r` in bash -c), all fixed across `8d47afa` + `b14ac4c`.

**Architecture — hook-based state tracking:**
- Hook script `hooks/session_tracker.py` (in repo, **symlinked** to `~/.claude/hooks/` as of #11 — was `cp` before, which left it 5 weeks stale) — 7 hooks write JSON to `~/.claude/session-states/{sid}.json`
- Monitor reads hook state as tier-1 status source; warm refresh 11ms (vs 183ms cold transcript parse)
- Hook sets terminal title via `/dev/tty{N}` write: `{emoji} {name} ·{sid8}` — the `·{sid8}` marker is how jump-to-terminal finds windows
- **TTY write gated on state/title change** (2026-03-24) — unconditional writes corrupted user input when background workflows fired hooks mid-typing
- **Delayed re-write on Stop** (2026-03-27) — Claude overwrites the `·{sid8}` marker with its auto-summary AFTER the Stop hook returns; spawn `sleep 0.5 && printf` background process to reclaim it
- **Transcript custom-title is canonical (#10)** — hook reads transcript `custom-title` line on Stop and gives it precedence over `/tmp/claude-name-{sid}` (which lags one statusline render after `/rename`); hook + monitor + window title now share one source

**Status detection (#7, #11):**
- ◐ BUSY (cyan) when main loop idle but `subagents/**/workflows/**/*.jsonl` written in last 15s (rglob — workflow agents nest 2 deep)
- Hook idle but transcript mtime <5s → ● WORKING (slash commands can miss UserPromptSubmit so hook never flips)
- `_is_session_alive` hook-state fallback uses `_pid_is_claude` (rejects recycled PIDs — #6)
- Orphan-PID pass skips booting sessions (no name + started <5s) so no transient "Claude"/WORKING/0% ghost row

**Archive filter alive-check (2026-04-05):** `parse_sessions()` treated any transcript with mtime > 24h as "archived" and hid it by default. After sleep/wake, all idle sessions have stale transcript mtimes but live processes — monitor showed 0-1 sessions despite 15 running claude processes. Fix: before skipping an "archived" session, call `_is_session_alive()`; if true, keep it as an active session regardless of transcript mtime. Reclassifies archived→active when process is live.

**7-day archive cutoff + pin (2026-05-26):** distinct second band. `claude_monitor.py` (~L696-729) has `active_cutoff` 24h and `archive_cutoff` 7 days. A session with no live process whose transcript mtime is >7 days old is hidden even with `show_archived: true`, and is NOT in `monitor-hidden.json` (so it is not a Delete×2 accident). The **pin** is the designed escape hatch from the 7-day cutoff. When a `tools-*` row vanishes, diagnose in order: confirm not in `monitor-hidden.json` → check transcript mtime age → if >7d and you want it kept, pin it. (Canonical crankit origin session = sid `1c8b0707`, cwd `/Users/mk` not `/code/crankit`, renamed melt-blitzit→tools-crankit — which is why project-dir searches miss it.)

**Jump-to-terminal (two-phase matching, 2026-03-31):** Discovery via `Application("Ghostty").windows.name()` (sees ALL spaces). Phase 1: find `·sid8` match and best name-based match independently. Phase 2: if both point to the same window, use it; if they point to different windows, `·sid8` marker is stale on a dead tab — prefer the name match. Logs `DIVERGE:stale_sid_marker`. Name matching uses exact match (strips emoji prefix and `·sid8` suffix, compares bare name) to prevent "strategy" from matching "strategy-patterns" (commit `8a81652`).

**`_is_session_alive()` (updated 2026-03-31):** PID file in `~/.claude/sessions/*.json` → hook state PID fallback → recently-resumed grace period. Forked sessions may lack PID files; hook state PID check added.

**Ghost filter cache guard (2026-03-31):** When `tokens_out ≤ 20` but transcript file is >50KB, force rescan before filtering. Prevents `stale_ok` cached results from permanently hiding active sessions.

**Rename consolidation (2026-04-05):** `n` → opens RenamePrompt modal → sends `/rename <name>` directly. Context menu's "Rename…" triggers same flow. Optimistically updates hook state file so monitor shows new name immediately.

**Jump JXA — proc.frontmost not app.activate() (`02bfe3c`, 2026-04-10):** `app.activate()` could space-switch to Ghostty's key window and race the menu click on multi-display. Now uses `se.processes.byName(app).frontmost = true` (no space switch). Window-menu search scoped to the window-list section (after last separator).

**Auto-rename guards (`d43ab16`, 2026-04-07):** Verify `proc.windows[0].name()` contains the target `·sid8` before typing `/rename` — aborts with `DIVERGE:auto_rename_wrong_front` if raise landed wrong. Skips junk titles.

**Gruvbox themes (`2605490`, 2026-04-08):** `GRUVBOX_DARK`/`GRUVBOX_LIGHT` Theme objects mirror ghostty themes. Auto-follows macOS appearance unless saved pref. `t` toggles.

**`P` broadcast (`eea1e87`, 2026-04-11):** `_resolve_cursor_group()` finds group at cursor; `_broadcast_command()` sends `/proactive` to live members, then raises monitor. Refuses on `ungrouped`.

**Kill + tab-close (2026-04-12):** `_kill_and_close_tab()` worker — raise window while `·sid8` marker intact, verify frontmost, SIGTERM, sleep 0.6s, re-check frontmost has NO `·sid8` marker, type `exit`, raise monitor.

**Mouse + multi-select (#8, #9):** SessionTable subclass owns clicks — single-click highlights, double-click (`Click.chain==2`) jumps, Enter opens menu. Shift+Up/Down/Click extends selection (anchor-based, in-place cell restyle). Delete×2 hides selection (history mode, archived/closed only) → `~/.claude/monitor-hidden.json`. Cursor lands on nearest survivor after hide. View state (sort/toggles/view) persists to `monitor-prefs.json` across Shift+R. CLAUDE.md rule #4: preserve cursor after row mutations.

**Layout (#11):** Session column elastic — width = terminal − Σ(other rendered widths) − padding, floored 20. Runs on resize and post-refresh. Duplicate row titles get `·{sid8}` suffix.

**Context % (#11):** Ground-truth from `/tmp/claude-ctx-{sid}` (statusline-written). Fallback estimate uses `MODEL_CONTEXT_WINDOW[model]` (1M for 4-6, 200k older) and renders `~N%`.

**Launcher (#11):** `~/.local/bin/claude-monitor` calls `.venv/bin/python` directly (offline-safe, PATH-free for Ghostty `command:` spawn). `jumpback` launches a new Ghostty monitor window if no `·MONITOR` window found.

**Python pinned 3.12 (Santa):** `.python-version` and `pyproject.toml` pin Python 3.12, not upstream's 3.14 (macOS SIGKILLs unsigned uv-managed 3.14; see `reference_macos_python312.md` and the CLAUDE.md Santa blocklist). No 3.13+ features in use. On upstream pulls that reset `>=3.14`, relax back to `>=3.12` and regenerate `uv.lock`.

**Other features:** Kanban (`k`), spinner ping-pong `·*✢✳✶✻` @132ms #D97757, restart (`R`), jumpback (Ctrl+Shift+Space via skhd), grouping (`g`), subagent lazy-load (6.4s→0.8s cold), `cursor_row=-1` guard (`de3a361`), search hint in StatsBar.

**`/catchmeup` fix (2026-04-23):** `session_context.py` now uses PID-ancestry to identify current session (was guessing by newest-thinking timestamp; had misidentified monitor session as spacenames).

**main at `1da0a31` (2026-05-11). 224 tests passing. All feature branches pruned. See `left_off.txt` for follow-ups.**

**v2 truth layer (2026-06-01, PRs #16 + #18):** v2 foundation = single source of truth for session identity (truth layer), `&ignore` marker, docs; mk's Ghostty config copied into the repo for 1:1 adopters; Beck got the Slack walkthrough. **Wrong-rename bug fixed (#18):** `claude --resume <sid>` could rename a DIFFERENT live session (mk's resume of 37afe42d brought up EBC-NAB but renamed it tools-monitor); auto-rename now verifies identity before typing /rename; stale duplicate killed. mk on the loop: "The crystallization loop you asked for is doing exactly its job."

**Search and sibling rows (2026-06-15/16, PRs #21-23):** `/` opens search; while the box has focus every printable key is filter text, not a hotkey. **`↓` drops focus into the table KEEPING the filter** (so you navigate matched rows with hotkeys); **Esc clears the filter and restores all rows**. Sibling rows (same conversation resumed in two pids) are disambiguated `·{sid8}·{pid}`: #22 first appended the carrier-key suffix verbatim (`@pid`), but `@` is the explicit-group sigil in `_group_key()` so the siblings re-keyed away from their group, broke the contiguous-group invariant, and `_refresh_apply` DuplicateKey'd on the second `__group__config` header; #23 swapped to the group-key-neutral middle-dot and added `test_disambiguation_preserves_group_key`.

**Background-tab jump and rename (2026-06-21/25, PRs #24, #31):** A session that lives as a background Ghostty tab is invisible to `app.windows.name()` (active-tab title only), so jump returned `no_match` while the process was alive (`DIVERGE alive_but_unfound`; mk's config-skills was the third tab in the tools-monitor window). #24 added a Phase-3 fallback that walks `AX tabGroups[0].radioButtons()`, clicks the matching tab, raises the window, and re-stamps the `·sid8` OSC marker in `_heal_hook_state`. #24 refused all typing on tab matches; that was over-conservative because rename FROM the monitor always lands in Phase 3 (the monitor is the foreground tab). **Harry Liu's #31** collects tab hits first and types only when exactly one carries the `·sid8` marker, mirroring Phase-1's `abort_multi_sid` guard; name-only and duplicate-sid still refuse.

**UX polish (2026-06-21/25, PRs #25-30, #32):** #25 successful jump or resume clears the search filter so the full table is showing on return. #26 statusline re-stamps the OSC tab title the instant `session_name` changes (so `/rename` updates the Ghostty tab immediately, without waiting for a state-transition hook). #27 `i` toggles the bottom preview panel, choice persists in `monitor-prefs.json`. #28 a `--` in the title is the **group-lead marker**: `config--LEAD` floats to the first row under its group header in every sort mode (stable secondary sort over the mode sort), implementing [[reference_lead_summon_pattern]] in the monitor. #29 stabilized #25's pilot tests (the jump-clears-search test leaked a `refresh_sessions` worker into the next test, causing random `NoMatches '#session-table'`). #30 preview panel renders its body via `rich.markdown.Markdown` (tables draw as boxes, code fences get syntax colour). #32 the API-key hint says "Shift+K" (lowercase `k` is unbound since kanban moved to `v`).

**Adopters/contributors:** Harry Liu (Applied AI Architect, GTM Field Kit) onboarded 2026-06-16, first external PR (#31) merged 2026-06-25. main at 294 tests.


## Relocated from auto-memory 2026-07-16: project_monitor_diverge_logging.md

---
name: claude-monitor closed-loop bug detection via DIVERGE log category
description: DIVERGE log entries self-detect when monitor telemetry disagrees with ground truth; 7 DIVERGE types implemented as of 2026-04-12; heartbeat/retry loops pending
type: project
originSessionId: 7a5651f1-bf71-45a2-a208-781f6bdf36ab
---
Monitor self-detects its own bugs by logging when its telemetry diverges from observable ground truth. Core challenge: the monitor IS the telemetry layer, so there's no higher authority — but there are multiple independent sources for most facts, and disagreement between them is a detectable signal.

**Implemented DIVERGE types (as of 2026-04-12):**

1. **`wrong_window`** — Post-jump: `·{sid8}` in raised title ≠ target sid8.
2. **`alive_but_unfound`** — Jump found no window, but `_is_session_alive()` is True. Refuses resume (would duplicate).
3. **`stale_sid_marker`** — Two-phase jump: `·sid8` and name match point to different windows. Name wins; marker stale on dead tab.
4. **`auto_rename_no_new_window`** — Post-resume poll (15s) saw no new `·sid8` marker. Hook didn't fire or resume failed.
5. **`auto_rename_wrong_front`** (`d43ab16`, 2026-04-07) — After raising the resumed window, frontmost title lacks the marker. Aborts the `/rename` keystroke instead of typing into whatever has focus.
6. **`kill_tab_close_abort`** (2026-04-12) — After SIGTERM + sleep, frontmost title carries a `·sid8` marker (some live session). Aborts the `exit` keystroke.
7. **`kill_raise_wrong_front`** (2026-04-12) — Kill flow raised a window but frontmost lacked the target marker. Process is still killed; tab-close skipped.

**Proposed but not yet implemented:**
- **Retry-as-signal** — same session jumped twice within 10s = first jump failed visibly
- **Invariant heartbeat** — every 30s, cross-reference: every `working` session has fresh transcript mtime; every `·{sid8}` window title has a live state file; every state file PID is alive

**Jumpback feature (`b14ac4c` → `0be1822`):** Monitor stamps `◇ Claude Monitor ·MONITOR` on its own terminal title via `/dev/tty` write in `on_mount()`. Standalone script at `~/.local/bin/jumpback` raises the monitor window. Bound to **Ctrl+Shift+Space** via skhd. First 20 launches show an onboarding toast teaching the hotkey.


## Relocated from auto-memory 2026-07-16: reference_claude_monitor.md

---
name: claude-monitor repo and custom statusline
description: User's claude-monitor TUI and custom Claude Code statusline — repo location, GitHub origin, statusline config, hook-based state tracking, jump-to-terminal via TTY title marker
type: reference
---

`claude-monitor` is cloned at `/Users/mk/Projects/claude-monitor` from GitHub repo `maxtomachine/claude-monitor`.

It includes a custom Claude Code statusline script at `statusline/statusline.sh` (two-line HUD with context bar, session marker, tool, token counts, cost, effort level, fast-mode indicators).

The statusline is configured in `/Users/mk/.claude/settings.json` as:
```
"statusLine": {"type": "command", "command": "bash /Users/mk/Projects/claude-monitor/statusline/statusline.sh"}
```

**Hook-based state tracking (added 2026-03-19):** Hook script at `~/.claude/hooks/session_tracker.py` writes JSON state files to `~/.claude/session-states/`. Configured in `~/.claude/settings.json` with **7 hook entries** (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest, Stop, SessionEnd). Monitor reads these as tier-1 status source in `determine_status()`. Hook idle state decays to "idle" in monitor after 5 minutes via `state_entered_at` timestamp. State files GC'd hourly (exited >24h deleted).

**Hook state fields:** `session_id`, `state`, `state_entered_at`, `timestamp`, `cwd`, `pid`, `tty`, `started_at`, `tool`, `tool_target` (file_path/command/pattern/url extracted from tool_input), `title`, `title_source`, `title_updated_at`.

**Jump-to-terminal (v2→v3, 2026-03-19→27):** Hook writes terminal title directly to `/dev/tty{N}` on every Pre/PostToolUse event, formatted as `{emoji} {name} ·{sid8}`. The `·{sid8}` marker is unique per session. TTY discovered by walking the hook's process tree up to `claude` and reading its controlling terminal. Monitor's `_resolve_match_candidates()` builds a candidate list (·sid8 first, then name sources). **v3 two-phase matching (2026-03-27):** JXA independently finds the `·sid8` window and the best name-window. If they agree → use sid. If they disagree → `·sid8` is stale on a dead tab, prefer the name match, log `DIVERGE:stale_sid_marker`. The `+sid_stale` suffix on `matchedCand` is stripped for Window-menu matching via `.split("+")[0]`. Details in `JUMP_TO_TERMINAL_BUG.md`.

**Root cause v1 failed:** Claude Code sets terminal titles to auto-generated summaries (e.g. "Build Claude Code session monitor dashboard") that don't match `session_name` in the status JSON or any stored source. No stable string existed to match against — hence the TTY-write approach where WE set the title.

**Title sources (priority order):** custom_title (user-set) → hook state title (session-memory or Haiku-generated) → statusline name (`/tmp/claude-name-{sid}`, written by statusline from Claude Code's `.session_name`) → sessions-index summary → session-memory/summary.md (direct read) → first prompt → cwd → session_id. The statusline source was added 2026-03-27 — without it, sessions launched from skill directories (cwd = `/Users/mk/.claude/skills/{name}`) showed the skill dir name instead of the session name. Hook also auto-updates when `title_source == "statusline"` and the name file changes (handles renames).

**Haiku title fallback:** API key at `~/claude-monitor/.api_key` (gitignored). Hook spawns background Haiku call for sessions 2+ minutes old without a title.

**Hook-only fast path (added 2026-03-19):** `scan_full_file(path, stale_ok=True)` returns cached transcript scan even if mtime changed, when hook state timestamp is <30s old. Warm refresh dropped from 183ms → 11ms (16x). Cold scan still ~180ms. Hook provides fresh status/tool; tokens/model/cost are slow-changing.

**Statusline line 2 (repurposed 2026-03-19):** For employee auth (no quota bar), shows `·{sid8}  {tool}  $cost` — visual confirmation of hook state + jump marker. Consumer auth still shows quota bar.

**"Doing" column sources:** Tier 1 = hook state `tool`+`tool_target`, Tier 2 = transcript-derived `last_tool`+`last_tool_input`, Tier 3 = assistant text gerund extraction.

**Rename action (added 2026-03-19):** `n` keybinding or menu option sends `/rename` to selected session's terminal via `_send_to_terminal_session()` (raises window + JXA keystroke). Patches unnamed/orphaned sessions — Claude auto-generates a title on `/rename`.

**`_is_session_alive()` PID resolution chain (updated 2026-03-27):** (1) `~/.claude/sessions/*.json` PID files — primary, refreshed every 2s. (2) Hook state file PID (`read_hook_state(sid)["pid"]`) — fallback for sessions without PID files. (3) `_recently_resumed` grace period (60s) — covers the window between resume and PID file creation. Some sessions (observed 2026-03-27) never create PID files but the hook tracks them; without the hook fallback they were permanently invisible.

**Ghost session filter:** Sessions with `tokens_out ≤ 20` are hidden unless `_is_session_alive()` confirms a live process. **Stale-cache guard:** when `tokens_out ≤ 20` but the JSONL file is >50KB, the scan cache is force-invalidated before filtering — prevents `stale_ok` cached results from permanently hiding active sessions whose `tokens_out` was 0 at first scan.

**Resume fallback (guarded 2026-03-27):** When jump can't find the window, checks `_is_session_alive()` first — if PID is alive, REFUSES to resume (would spawn duplicate), logs `DIVERGE:alive_but_unfound` with candidate list, and shows warning toast suggesting `/rename`. Only resumes if PID is dead. Opens a new tab (cmd+T via JXA) and types `claude --resume {sid}`. Tries Ghostty → iTerm2 → Terminal.app.

**Jumpback (added 2026-03-27):** Monitor stamps its own terminal title as `◇ Claude Monitor ·MONITOR` on mount via direct `/dev/tty` write (Textual captures stdout, so `print()` of the escape never reaches the terminal — see `feedback_textual_captures_stdout.md`). Standalone script at `~/.local/bin/jumpback` (symlink to repo's `jumpback`) raises the monitor window from anywhere using the same Window-menu-click approach as jump-to-session. Bound to **Ctrl+Shift+Space** via skhd (`~/.config/skhd/skhdrc`). `install.sh` handles: `brew install koekeishiya/formulae/skhd` (needs the tap, not in core brew), appends keybind, runs `skhd --start-service`. skhd requires Accessibility permission (System Settings → Privacy & Security → Accessibility) — macOS prompts on first keypress. skhd config live-reloads; no Ghostty restart needed. Ctrl+Space was rejected because it's macOS's default input-source-switch hotkey when multiple keyboard layouts are enabled.

**Terminal support:** All JXA window ops (jump, send keystroke, resume) scan Ghostty, iTerm2, and Terminal.app in order. Added iTerm2 on 2026-03-20.

**Distribution-ready (2026-03-20):** Hook script lives in repo at `hooks/session_tracker.py`. `install.sh` installs hook + adds hooks config to settings.json + enables `workspaces-auto-swoosh` (macOS setting required for cross-space jump). README rewritten with current features (kanban, hooks, spinner, theme, rename, restart). Python badge updated to 3.12+.

**Test config:** `addopts = "-p asyncio"` in pyproject.toml explicitly loads pytest-asyncio (env has `PYTEST_DISABLE_PLUGIN_AUTOLOAD=true` which otherwise blocks it). The `]2;Claude Monitor` test-output noise was our own `print()` in `on_mount()` — now guarded by `PYTEST_CURRENT_TEST` env check. tmux e2e tests at `tests/test_tmux_e2e.py` (flaky, skipped in CI).

**Kanban view (added 2026-03-19):** `k` opens full-screen modal with 5 status columns (left→right: Closed→Idle→Waiting→Approval→Working). Cards are **minimal** — title (hyphen-wrapped) + activity only (no project/model/cost). Zero padding/margin CSS for maximum density (tightened 2026-03-24). Arrow-key nav (←→ skip empty cols, ↑↓ wrap). Enter stacks SessionMenu on top; escape returns to kanban. Subagents excluded. Spinner in Anthropic orange (#D97757). Working column index tracked via `_working_col` (not hardcoded).

**Current keybindings:** `q` quit, `r` refresh, `s` sort, `a` subagents, `z` archived, `c` columns, `k` kanban, `t` theme, `R` restart (os.execv), `j` cursor down, `n` rename, `/` search, `Enter` menu.

**Effort/fast indicator resolution:** Fast mode reads `settings.json:fastMode` directly (single jq call) — it's the LIVE toggle state updated by `/fast`, NOT a startup default. `output_style.name` in the status JSON is unrelated (tracks rendering style). Effort: transcript-parsed last `/effort` command → settings.json `effortLevel` fallback. **Gotcha:** `effortLevel` key can silently disappear from settings.json when CC rewrites the file (e.g. `/model`). Clear caches if stale: `rm -f /tmp/claude-sl-*.cache`. See `feedback_config_defaults_at_spawn.md` for the 3-round bug history.

**Status JSON fields (2026-03-20 inventory):** `context_window, cost, cwd, exceeds_200k_tokens, model, output_style, rate_limits, session_id, session_name, transcript_path, version, workspace`. No effort_level, no fast_mode. To inspect raw JSON: temporarily add `echo "$input" > /tmp/claude-statusline-input.json` to statusline.sh (don't leave in — review-branch flagged as security concern).

**extra_usage.used_credits:** Cumulative **cents** account-wide — not per-session. Statusline snapshots at first call (`~/.claude/session-states/{sid}.extra-snap`) and shows delta. Moved from /tmp on 2026-03-24 (reboot cleared /tmp → all sessions showed same delta). Divide by 100 for dollars. Noise for employee accounts (no billing).

**Statusline clear-to-EOL (2026-03-24):** Each printf ends with `\033[K` before `\n` to clear stale text when input area grows and pushes prompt fragments into statusline rows.

**JXA window ops (unified 2026-03-20):** Single function `_raise_window_by_content(session, then_text="")` handles both jump and send-keystroke. `_send_to_terminal_session` and `_close_terminal_tab` are thin wrappers. One JXA call per operation.

**KanbanView hotpath (fixed 2026-03-20):** Card bodies (title + activity) precomputed in `__init__` as `list[tuple[Session, body_str]]`; spinner tick only swaps the icon glyph. Was calling `generate_activity()` → `read_hook_state()` at 132ms intervals.


## Relocated from auto-memory 2026-07-16: reference_dbieber_session_tracker.md

---
name: dbieber session tracker as improvement reference
description: dbieber's hook-based session_tracker.py (anthropics/dotfiles) — source of patterns adopted into claude-monitor on 2026-03-19
type: reference
---

dbieber's Claude Code session tracker: hook script that writes JSON state files to `~/.claude/session-states/` on each Claude event, polled by a separate `claude-sessions` monitor.

**Source**: `https://github.com/anthropics/dotfiles/blob/main/users/dbieber/.claude/hooks/session_tracker.py` and `.../bin/claude-sessions`

**Patterns adopted into claude-monitor (2026-03-19):**

1. **Hook-based state tracking** — adapted into `~/.claude/hooks/session_tracker.py`. Simplified: no S3/fishfile, no tmux, no stable IDs. Local-only, writing to `~/.claude/session-states/`.

2. **Session title from `session-memory/summary.md`** — added to both hook (on stop events) and monitor (`read_session_memory_title()` in `build_session()`).

3. **Haiku API title fallback** — adopted in hook. Background subprocess calls Haiku for sessions 2+ min old without a title. Key at `~/claude-monitor/.api_key`.

4. **State guards** — adopted: `exited` is terminal, subagent `PreToolUse` can't flip idle→thinking, session-memory file edits ignored.

**Extended beyond dbieber's version:**
- **TTY discovery + title marker** — hook walks process tree to find `claude`'s TTY, writes `{emoji} {name} ·{sid8}` to `/dev/tty{N}`. The `·{sid8}` is a unique jump-to-terminal match key that dbieber's version doesn't have (his uses tmux session/window fields instead).
- **PID detection via comm-match** — walk up until `comm` contains "claude", rather than fixed grandparent. dbieber's grandparent logic found the wrong PID (login shell) in this environment.

**Not adopted**: S3 sync (fishfile is dbieber-specific; SSH planned for later), stable IDs (monitor already has project/session derivation), tmux integration (user uses Ghostty window titles), debug logging.


## Relocated from auto-memory 2026-07-16: reference_tty_terminal_title.md

---
name: Set terminal title from subprocess via /dev/tty{N}
description: Technique for setting Ghostty/terminal window titles from a subprocess that doesn't have direct stdout → terminal; write OSC 2 escape to the TTY device file
type: reference
---

To set a terminal window title from a subprocess whose stdout isn't connected to the terminal (e.g., a hook script spawned by Claude Code, whose output is captured by the parent):

```python
with open(f"/dev/{tty}", "w") as f:
    f.write(f"\x1b]2;{title}\x07")
```

Where `tty` is the TTY device name (e.g. `ttys015`) obtained from the parent process's controlling terminal:

```python
result = subprocess.run(["ps", "-p", str(pid), "-o", "tty="], capture_output=True, text=True)
tty = result.stdout.strip()  # → "ttys015"
```

**Why this works:** The TTY device file is writable by the terminal's owner regardless of which process writes to it. The OSC 2 escape sequence (`\e]2;{title}\a`) is interpreted by the terminal emulator as a window title change. `/dev/tty` (no suffix) requires a controlling terminal; `/dev/tty{N}` (explicit device) doesn't.

**Why this mattered (claude-monitor, 2026-03-19):** Claude Code sets terminal titles to auto-generated summaries that aren't stored anywhere readable. To make jump-to-terminal work, the hook script needed to set a title WITH a known marker (`·{sid8}`). The hook's stdout is captured by Claude's hook system, so writing the escape there doesn't reach the terminal. Writing to the explicit `/dev/tty{N}` device bypasses the capture.

**Gotchas:**
- `/dev/tty` (no suffix) fails with "Device not configured" when the subprocess has no controlling terminal — use the explicit device path instead.
- If another process (e.g., Claude Code) also sets the title, whoever writes last wins. Fire on frequent events (PreToolUse + PostToolUse) to keep your title dominant.
- Verified working on macOS Darwin 25.3.0 with Ghostty. Should work on any POSIX terminal.
