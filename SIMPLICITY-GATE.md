# Simplicity Gate — Session Truth Layer

The no-tradeoff success bar says the re-architecture must be *simultaneously* more
correct and simpler. "Simpler" is measured, not asserted. **Gate: cumulative net
non-test LOC in `claude_monitor.py` + `hooks/session_tracker.py` must be negative
by the end of Phase 3.** If the Phase 3 projection is not net-negative, descope
`agents --json` to status-enrichment-only and re-plan rather than ship more code
while claiming "simpler."

Scope of the count: production code only (the two files). Tests and docs are
excluded; adding tests is a feature, not a cost.

## Source-to-field replacement matrix (what collapses into the resolver)

| Today (independent derivation) | Becomes | Phase |
|---|---|---|
| `build_session` inline title chain + live/exited branch | `resolve_session()` title tier | 1 |
| `determine_status` body | `resolve_session()` status tier (fn kept as thin wrapper) | 1 |
| `_is_session_alive` body | `resolve_session()` liveness tier (fn kept as thin wrapper) | 1 |
| `parse_sessions` pid-orphan pass | `_enumerate_live_instances()` | 3 |
| `parse_sessions` `sid@pid` split pass | resolver returns N instances by key | 3 |
| `_refresh_pid_map` (full scan) | fallback-only path under the enumerator | 3 |
| `reconcile_sources` / `_reconcile_sessions` | lightweight invariant logger | 3 |

## Projection (replace with actuals at each merge)

| Phase | Adds (est) | Deletes (est) | Net (est) | Net (actual) |
|---|---|---|---|---|
| 1 resolver + ResolvedIdentity + base_sid + invariant/log harness | ~+260 | ~-110 | ~+150 | TBD |
| 2 hook sibling-record I/O + resolver dead-case + backfill | ~+120 | ~-10 | ~+110 | TBD |
| 3 enumerator + capability-detect; delete the two passes + shrink reconcile | ~+70 | ~-360 | ~-290 | TBD |
| **Cumulative by end of Phase 3** | | | **~-30** | **TBD** |

Honest read: Phases 1-2 run positive (the resolver and the durable-record I/O are
real new code); the net-negative is realized only by the Phase 3 deletions. The
structural win in Phase 1 (N derivation sites collapse to one chokepoint) is real
but does not by itself reduce line count. The cumulative figure is what the gate
judges. Measure with `git diff --stat <merge-base>..HEAD -- claude_monitor.py hooks/session_tracker.py`
at each phase PR and record the actual Net above.

## Correctness ledger (the other half of the no-tradeoff bar)

The self-auditing chokepoint: `resolve_session()` asserts invariants on every
refresh and logs violations (with the raw multi-source snapshot) to
`~/.claude/monitor.log`. Each logged violation becomes a fixture in
`tests/test_resolve.py`. The invariant set only grows; correctness ratchets.

Invariants (v1):
1. Every live instance resolves to exactly one row (no dupes).
2. Every rendered row key normalizes (`base_sid`) to exactly one conversation sid.
3. No two rows share an `instance_id`.
4. Every title has a known `title_source` (never "guessed"/empty for a live row).
5. Every pin matches exactly one grain (bare / `@pid` / `#started_ms`), never zero-when-present or many.

Grep the field after deploy: `grep '"cat":"invariant"' ~/.claude/monitor.log`.
