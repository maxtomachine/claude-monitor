# Session Truth Layer Re-architecture — Final Plan

> Produced by plan-hunter (4 drafts, 4 judges). Winner: MVP-first (8.6/10), with
> grafts from risk-first (simplicity gate, net-deletion ledger) and user-first
> (freshness-gated title, alive-at-deploy backfill). All keystone facts verified
> against the machine.

## Verified findings that shape the plan
- pid files carry `startedAt` as int-ms (14/14, e.g. `1779468421760`), so the deterministic surrogate holds.
- `claude agents --json` returns 14 entries with keys `{cwd,kind,name,pid,sessionId,startedAt,status}`; 3 lack `name`; statuses are `busy`/`idle`.
- `resolve_session` is absent (0 hits). Cited defs match: build_session 956, determine_status 1488, _is_session_alive 1127, parse_sessions 689, reconcile_sources 1258, _save_sid_set 103; hook write_state 314, mark_exited 462, get_local_state_file 96.
- Hook records key by `{sessionId}.json`, so `--resume` clobbers; no `instance_id` field today.
- pins = list of 30 (2 with `@`); hidden = 36. `claude` binary at `/Users/mk/.local/bin/claude`.
- **Correction:** the venv is Python 3.12.13 (`requires-python>=3.12`). The project CLAUDE.md claim of "3.14+" is wrong; fix it in Phase 0.

---

## Confirm before starting (substrate decisions; day-60 rework cost)

**1. Keying model: carrier (recommended) over split-brain.** The winning draft kept `session_id = bare sid` and added a separate `instance_id`, migrating every cockpit map. The recommended **carrier** model instead: `session_id` *carries* the unique row key (`{sid}#{startedAt_ms}` for live rows), and a new `Session.sid` field holds the bare conversation id for file lookups. Why: (a) it generalizes the pattern the code already uses (split rows are keyed `sid@pid` today precisely to stay unique), so cockpit maps (`_pinned`, `_bell`, `_selected_key`, `_prev_statuses`, DataTable row key) are left untouched, which is the must-not-regress surface; (b) a missed migration in a *file lookup* degrades gracefully (jump falls back to lsof), whereas a missed migration in a *cockpit map* is the exact desync bug we are killing, so carrier moves residual risk to the safer location; (c) it fixes a latent bug where `_pid_map.get(session.session_id)` returns `"sid@pid"` and always misses for split rows.

**2. On-disk reshape: sibling per-instance record.** Writing `instance_id` into the existing `session-states/{sid}.json` does not survive `--resume` (verified clobbered). The hook instead writes a sibling at `~/.claude/session-states/instances/{sid}#{startedAt_ms}.json`. Code stays single-file + hook.

**3. Separator `#` for the surrogate.** `{sid}#{startedAt_ms}`. Collision-free against `@` (used by title grouping and the 2 legacy `sid@pid` pins); gives tri-state pin disambiguation.

**4. Ship cadence: four sequential `/devcycle` merges**, not one mega-PR. Phase 1 is a complete bankable savepoint; Phases 2-4 are independently shippable fidelity layers. Matches the v1-savepoint-then-summit default; limits blast radius against protected main.

Smaller: (a) gate the final merge on a manual Shift+R live check; (b) RFC in the Phase 1 PR or standalone.

**Already verified, no action needed:** surrogate derivable from disk; `agents --json` is supplementary not sole-source (no tokens/cost/title, 3/14 lack name); resolver is greenfield; binary fallback `/Users/mk/.local/bin/claude`; WIP is the exited-straggler precedence change plus its test; pins/hidden are string lists; Python target is 3.12.

---

## Thesis

The headline win (one key plus one resolver makes the bug class structurally impossible, with net code deletion) does **not** require the hook change or `agents --json`. The file `~/.claude/sessions/{pid}.json` already carries `sessionId + startedAt + status + name` (exactly what `agents --json` returns, same `startedAt`). So the durable surrogate `{sid}#{startedAt_ms}` is derivable from data already on disk, and `resolve_session()` over a uniform instance key is the standalone **MVP (Phase 1)**. Writer-of-record durability (Phase 2) and authoritative live enumeration (Phase 3) are independently-shippable layers on top. Bank Phase 1 first.

Code changes land only in `claude_monitor.py` and `hooks/session_tracker.py`. Tests are separate files. Function-def line numbers are verified; finer UI key-site refs are a map to re-confirm at edit time.

---

## Phase 0 — Clean baseline, fold the WIP, set the simplicity gate (~30 min)

The dirty tree is the exited-title-straggler precedence change in `build_session` plus its test in `tests/test_parsing.py`, plus `uv.lock` cross-machine churn. The precedence change is literally the first row of the precedence table, so fold it in deliberately. Revert the lock churn.

▶ copy below
```bash
git -C /Users/mk/Projects/claude-monitor checkout uv.lock
git -C /Users/mk/Projects/claude-monitor switch -c feat/session-truth-layer
git -C /Users/mk/Projects/claude-monitor add claude_monitor.py tests/test_parsing.py
git -C /Users/mk/Projects/claude-monitor commit -m "Prefer settled hook title over transcript straggler for exited sessions"
cd /Users/mk/Projects/claude-monitor && uv run pytest tests/ -q
```
◀ copy above

Then, before writing the resolver:
1. Confirm the suite is green on the branch with the WIP folded.
2. Fix the stale runtime claim: change the project CLAUDE.md "Python 3.14+" line to 3.12.
3. Golden snapshot (the parity oracle for Phase 4): capture `claude agents --json` and current `session-states/` + `sessions/` listings into a gitignored `/Users/mk/Projects/claude-monitor/.golden/` (not `/tmp`, which clears on reboot).
4. Simplicity gate (net-deletion projection, grafted from risk-first): produce a source-to-field replacement matrix and a projected deleted-vs-added line estimate. The gate: cumulative net LOC must be negative by end of Phase 3. If the projection is not net-negative, descope `agents --json` to status-enrichment-only and re-plan rather than shipping more code while claiming "simpler." Measure actual `git diff --stat` at each phase merge against this projection.

---

## Phase 1 — MVP: instance key + `resolve_session()`, files-only (~1 day)

Delivers the entire headline for live and exited cases: dupes, title stragglers, ghost rows, status desync, pinned-but-invisible all become structurally impossible. No hook change, no `agents --json`.

### 1.1 Durable surrogate schema
- `instance_id = f"{sid}#{started_at_ms}"`. `started_at_ms` from the pid file's int-ms `startedAt` (verified 14/14). `pid` deliberately excluded (pids recycle; ms launch time per sid does not).
- Helper `base_sid(key) -> key.split("#")[0].split("@")[0]` normalizes all three keyings (bare, `@pid` legacy, `#started_ms` new) to the conversation sid.
- Collision guard (accepted-risk + detection): two instances of one conversation launched in the same millisecond would collide. Effectively impossible, but detect (two live pids, same sid, same `startedAt`) and `mlog` it; tiebreak by pid only when detected.

### 1.2 The one resolver (greenfield)
Add a frozen dataclass `ResolvedIdentity(key, sid, instance_id, pid, started_ms, title, title_source, status, alive, cwd, origin, source)` where `origin in {live, backfilled, reconstructed}` and `source in {agents, pidfile, hook, transcript}`. Add `resolve_session(sid) -> list[ResolvedIdentity]` (one entry per live instance; one for the dead/single case). It reads only from already-cached maps (`_pid_map`, `_hook_state_cache`, `_scan_cache`) so it adds zero I/O and preserves the per-row/per-refresh perf budget. This cache-only constraint is non-negotiable.

### 1.3 Precedence table (the heart of the change)

**LIVENESS** (highest tier wins; tier 0 added in Phase 3):
| Tier | Rule |
|---|---|
| 0 | (Phase 3) present in `agents --json` live set, `kind==interactive` |
| 1 | `sessions/{pid}.json` exists AND `os.kill(pid,0)` ok AND `_pid_is_claude(pid)` AND that file's `sessionId == sid` (guards recycled pid + `/branch` reuse) |
| 2 | hook record pid alive, cross-checks as claude, sid matches |
| 3 | resume grace: `_recently_resumed < 60s` |
| 4 | else dead (closed; archived if mtime old) |
| Hysteresis | (Phase 3) do not flip alive->dead on one missing `agents --json` snapshot; require absence from BOTH agents-json AND pid-files, or N consecutive misses, before closing |

**INSTANCE_ID / KEY:**
| Tier | Condition | instance_id | origin |
|---|---|---|---|
| 1 | live, `startedAt` present | `{sid}#{started_ms}` from pid file | live |
| 2 | live, no persisted id (alive at deploy) | same deterministic surrogate | backfilled |
| 3 | dead, persisted `instances/{iid}.json` exists (Phase 2) | its `instance_id` | live/backfilled |
| 4 | dead, only a prior pid known (legacy) | key `= {sid}@{pid}` | reconstructed |
| 5 | dead, nothing | key `= sid`; iid `= {sid}#0` | reconstructed |

**TITLE** (first non-empty wins; freshness-gated, grafted from user-first):
- Live: transcript `custom_title` only if its timestamp is newer than the hook record's `title_updated_at` (a fresh `/rename` not yet absorbed) -> `agents name` (Phase 3; structurally beats a parentUuid-tree straggler) -> statusline name cache -> hook `title` -> idx.summary -> session-memory -> `firstPrompt[:60]` -> cwd basename -> `sid8`. The 3/14 no-name case falls through.
- Exited with settled hook title (generalizes the folded WIP): hook `title` -> `custom_title` -> idx.summary -> session-memory -> `firstPrompt[:60]` -> cwd -> `sid8`.
- Title derived only from transcript reconstruction (dead, no hook) sets `origin=reconstructed`.

**STATUS** (after liveness; subsumes `determine_status` 1488):
| Tier | Rule |
|---|---|
| 0 | not alive -> closed / archived |
| 1 | hook state: approval->needs_approval; thinking->working; idle->waiting (decay to idle after 300s via `state_entered_at`); exited-but-alive (subagent SessionEnd wrote parent file) -> fall through, do not trust `exited` while alive |
| 2 | transcript mtime < 5s -> working (preserves slash-command path) |
| 3 | (Phase 3) `agents` status busy->working, idle->idle/background. Hook state stays HIGHER than agents status (version skew + agents lacks approval/thinking granularity) |
| 4 | pid-file status busy/idle |
| 5 | time heuristic on `last_assistant_time` (<30s working, <300s waiting, else idle); background-activity -> background |
| guards | preserve subagent-exit-pollutes-parent and `/branch` same-pid-different-sid rules as explicit rows + tests |

### 1.4 Route every renderer through the resolver (the collapse)
- `build_session` (956): replace the inline title chain + `determine_status` call with resolver output; set carrier `session_id` to `instance_id` and `Session.sid` to the bare sid.
- `parse_sessions` third pass (the `sid@pid` sibling split): replace with `resolve_session` returning N instances keyed by `instance_id`.
- `_is_session_alive` (1127) and `determine_status` (1488): re-express as thin wrappers over the resolver (keep names/signatures so existing tests and call sites survive), then delete the duplicated bodies. The net-deletion proof point.
- Add `sid: str = ""` and `instance_id: str = ""` to the `Session` dataclass; `session_id` carries the unique row key.

### 1.5 File-lookup sites switch to `.sid` (carrier consequence, latent-bug fix)
Audit and switch every file/cache lookup from `session_id` to `.sid`: `read_hook_state`, `_pid_map` lookups, transcript path, `scan_cache`, statusline `claude-ctx-{sid}` / `claude-name-{sid}` reads, jump's `_find_claude_pid` (`_pid_map.get(session.sid)`), `_resolve_match_candidates` (`session.sid[:8]`). The OSC window-title marker stays `.{sid8}` (sid-grain), so jump matching is unchanged. Cockpit maps stay on `session_id` and are untouched.

### 1.6 Pin migration (loss-free, conversation-grain)
- Centralize as `_pin_matches(s, pinned) = s.session_id in pinned OR base_sid(s.sid) in {base_sid(p) for p in pinned}`. A pin survives resume (new instance_id, same sid via loose branch) and honors an explicit sibling pin (exact instance match). Loss-free across bare / `@pid` / `#started_ms`. Apply at every read site. Symmetric for the 36 hidden entries.
- `action_toggle_pin` (4803): pin live rows by `instance_id`, dead rows by `sid`; unpin removes both the instance entry and any matching legacy `base_sid` entry.
- Lazy normalize on next `_save_sid_set` (no destructive rewrite). Add `--migrate-pins` near `--reconcile` for a dry-run report.

### 1.7 Tests (new `tests/test_resolve.py`, reuse `test_reconcile.py` fixture pattern)
Table-driven, one case per precedence row. Plus: instance-id stability (two `startedAt`->two ids; pid recycle->new id); pin tri-state; golden master of current `parse_sessions` for the 14 live sessions, asserting `resolve_session` reproduces it except the enumerated intended fixes written as explicit new assertions (so the golden master does not lock in today's bugs). Cockpit regression in `test_tui.py`: pin survives a simulated resume refresh; cursor `_selected_key` survives refresh under instance keying; bell keyed per instance. Keep the suite green; net add ~20.

**Ship Phase 1 via `/devcycle`. This is the bankable MVP.**

---

## Phase 2 — Writer-of-record: hook persists the durable id (~0.5 day)

Go-forward fidelity for closed/pinned sessions after the pid file is deleted at exit.

### 2.1 On-disk shape (the durability fix)
- New `INSTANCE_DIR = ~/.claude/session-states/instances/`, declared in both files.
- Sibling per-instance record `INSTANCE_DIR/{sid}#{started_ms}.json` (deterministic filename, survives `--resume` because each instance gets its own file). Schema: `{instance_id, sid, pid, tty, started_ms, kind, state, state_entered_at, title, title_source, title_updated_at, cwd, last_event_at, exited_at, origin}`.
- `session-states/{sid}.json` keeps working (back-compat) and gains one pointer field `instance_id` = the currently-live instance for that sid.

### 2.2 Hook write protocol (`hooks/session_tracker.py`)
- No dependency on SessionStart `source` (this is why the deterministic surrogate matters; sidesteps the resume-source risk entirely). At `session_start` (and as a heal on any event if absent): resolve pid via the existing `find_claude_pid_and_tty` (23), read `sessions/{pid}.json` for `startedAt`, compute `instance_id`, write the sibling record (`state=idle`) and stamp the `instance_id` pointer into `{sid}.json`.
- Resume = new instance, naturally: a fresh process fires a new `session_start`, new pid, new `startedAt`, new `instance_id`; seal the prior open instance record.
- Ordering and idempotency: write the sibling record FIRST, flip the `{sid}.json` pointer LAST (so the monitor never reads a half-updated pair). Only write `instance_id` if absent or changed. Reuse the atomic-write + mtime clobber guard from `generate_title_background`.
- `mark_exited` (462): set `exited_at` on both the pointer file and the sibling record. The sibling record is the post-mortem the resolver trusts for exited sessions.

### 2.3 Resolver dead-case + monitor-as-backfill-writer
- Resolver instance-id tier 3 reads the persisted sibling record when no pid file exists.
- Backfill for sessions alive at deploy (missed `session_start`, grafted from user-first): when a live instance has no sibling record, the resolver synthesizes the deterministic id from live `startedAt`, and the monitor performs one idempotent persist (guarded by absence + a `backfilled` flag) so post-mortem survives even if that session exits before its next hook event. Also a `--backfill-instances` CLI.
- `reconcile_sources` (1258) gains an `instance_id_missing` discrepancy that `_reconcile_sessions` heals for live sessions.

### 2.4 GC and tests
- Extend the existing state-file GC to sweep `INSTANCE_DIR`; keep sealed records ~7 days (RFC post-mortem queryability needs them); pinned instances exempt.
- Hook unit tests (the hook has none today): mint/persist idempotently; sibling survives a simulated resume; backfill marks `backfilled`; missing-everything marks `reconstructed`.

**Ship Phase 2 via `/devcycle`.**

---

## Phase 3 — Authoritative live enumeration: `agents --json` + fallback (~0.5 day)

Replaces hand-scraped pid-file + `os.kill` enumeration with the first-party list where available. No hard dependency. `agents --json` is supplementary, not sole-source (no tokens/cost/title, 3/14 lack name); it replaces *liveness enumeration*, while transcript scan, hook, and pid-file compose the rest.

### 3.1 Binary + capability detection
- `CLAUDE_BIN`: `shutil.which("claude")` with fallback to `/Users/mk/.local/bin/claude` (the monitor may launch under launchd/Ghostty without `claude` on PATH).
- `_agents_json_capable()`: run `[CLAUDE_BIN, "agents", "--json"]` with `timeout=3` in the background refresh worker only, never the UI thread; parse JSON; cache the boolean per process; re-probe every ~10 min or on Shift+R. Any of non-zero exit / timeout / `JSONDecodeError` / `FileNotFoundError` / empty-when-pids-exist -> False -> fall back. One capable binary surfaces all running agents regardless of each agent's own version (verified). `mlog` the decision once.

### 3.2 Unified enumerator
- `_enumerate_live_instances() -> list[dict]`: if capable, return parsed `agents --json` rows; else fall back to the existing `SESSIONS_DIR` scan. Both paths yield `{sid, pid, name, status, cwd, started_ms}`, so `resolve_session` consumes one list. Cache on the existing ~2s cadence (mirror `_pid_map_ts`) so the node spawn (~0.17s) runs at most once per refresh.
- Wire as liveness/status tier 0. Hook state stays higher than agents status.

### 3.3 Net deletion (the simplicity payoff)
The big cut lands here: delete the pid-orphan pass and the `sid@pid` split pass in `parse_sessions`; `_refresh_pid_map` (1082) becomes fallback-only; reduce `reconcile_sources` / `_reconcile_sessions` to a lightweight invariant logger. Measure `git diff --stat`; cumulative LOC must now be net-negative per the Phase 0 gate, else descope.

### 3.4 Tests
Mock `subprocess.run` to (a) return the documented array -> uses it; (b) raise `TimeoutExpired` / exit 1 -> falls back. Capability-toggle matrix: run the full suite once with the adapter forced ON and once forced OFF; both green proves no hard dependency and asserts no behavioral diff between the two paths on the same fixture.

**Ship Phase 3 via `/devcycle`.**

---

## Phase 4 — Cockpit hardening + live parity (~0.5 day)

Preserve and confirm the layer above the truth layer. Verify by the five promises the TUI exists to keep: know who needs me now (status), identify by stable title, jump to it, pin/group it, trust row-count equals real-session-count.

1. Attention-pulse: `_bell` / `_prev_statuses` keyed by the carrier `session_id` (now the stable instance key); working->waiting still rings, per instance. Test the transition fires per instance.
2. Jump-to-terminal: resolver hands the pid directly, so `_find_claude_pid` / `_resolve_match_candidates` simplify and the latent `sid@pid` miss is gone; verify `claude-jump` + double-click raise the right window (OSC `.{sid8}` marker unchanged). Confirm `"{sid}#{ms}"[:8] == sid[:8]`.
3. Groups: `_group_key` (1839) operates on the title and splits `@` in titles like `bugs@disclosey`; confirm the instance key never leaks into the title (dedup suffix appends `.{sid8}`, not the key). Test a grouped + multi-instance mix.
4. Cursor/scroll persistence: honor CLAUDE.md rule #4; `_selected_key` is now an instance key, stable across refresh; re-verify the restore loop and `scroll_x/scroll_y`.
5. Live parity: after merge you press Shift+R (the monitor cannot restart your running process). Confirm across the ~14 real concurrent sessions: no dupes, titles correct, ~30 pins intact, statuses match, no ghost rows. Run `uv run python claude_monitor.py --reconcile` -> expect 0 discrepancies. Diff the live row set against the Phase 0 `.golden/` snapshot. End the report with the PR URL.

---

## Phase 5 — RFC (parallel track; draft from Phase 1, finalize after Phase 2)

Deliver as `/Users/mk/Projects/claude-monitor/RFC-durable-instance-session-id.md`; route it to the CC team (AKI / Outline / Slack; no external publication).

1. Problem: `sessionId` is conversation-grain, reused across every `--resume`/`--continue`; the transcript is a `parentUuid` tree, not a line; there is no durable per-interactive-run id, so every tool reconstructs identity from 5+ desynced sources. Evidence: this monitor's recurring bug class and the de-facto surrogate it now ships.
2. Proposal: first-party durable per-instance `instanceId`, minted per interactive run/resume.
3. Carriage: transcript lines (tag each line with the writing `instanceId`, which also solves branch attribution), `sessions/{pid}.json`, a hook env var `$CLAUDE_INSTANCE_ID`, a field in `agents --json` (live), and a persisted post-mortem record / `--include-exited` query (dead).
4. Semantics: grain = one interactive run; stable within a run; distinct per resume; survives process death; distinct from and joinable to `sessionId`.
5. Compatibility: additive; older readers ignore it.
6. Alternatives: `(sid, pid)` insufficient (pid recycles); `(sid, startedAt)` is the de-facto surrogate this project now ships, with the sibling instance record as the reference implementation.
7. Ask: adopt `instanceId`; the project's `{sid}#{startedAt_ms}` becomes a thin alias.

---

## Net-deletion ledger (simplicity, proven not asserted)

| Removed / collapsed | Location | Phase |
|---|---|---|
| `build_session` inline title chain + double branch | 956 | 1 |
| `determine_status` body -> resolver wrapper | 1488 | 1 |
| `_is_session_alive` body -> resolver wrapper | 1127 | 1 |
| `parse_sessions` pid-orphan pass + `sid@pid` split pass | 689 | 3 |
| `_refresh_pid_map` -> fallback-only | 1082 | 3 |
| `reconcile_sources` / `_reconcile_sessions` -> invariant logger | 1258 | 3 |
| Added: `resolve_session` + `ResolvedIdentity` + `_enumerate_live_instances` + hook sibling-record I/O | new | 1-3 |

Honest phasing: Phase 1 is roughly LOC-neutral (the resolver replaces scattered derivation; the structural collapse from N derivation sites to one is the real Phase-1 win); the cumulative net-negative is realized by the Phase 3 deletions. The Phase 0 gate measures this at each merge.

---

## Risks and non-goals

- `startedAt` collision unguarded beyond detection+log; accepted (sub-ms double-launch of one conversation is effectively impossible).
- Leaf-uuid / window grain dropped: the surrogate is `(sid, startedAt)`, not the full `(sid, pid, leaf-uuid, window)` tuple. Explicit non-goal.
- Dead history is epistemically lossy once the live PID-to-leaf binding is gone: legacy/dead mega-trees get conversation-grain best-effort reconstruction marked `reconstructed`. Go-forward fidelity only.
- Liveness flap from a single `agents --json` hiccup: mitigated by the hysteresis rule (Phase 3).
- Global, irreversible-in-flight hook across 14 multi-version live sessions: changes are additive-only, pointer-last-ordered, and the resolver's backfill removes any hard ordering dependency, so the hook can deploy without coordinating restarts.
- Protected main: branch -> PR -> `merge --admin`; full green suite + capability-toggle matrix + live parity must pass before each merge gate.
