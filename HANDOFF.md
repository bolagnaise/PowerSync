# PowerSync Session Handoff — 2026-04-15

## Repo
- **Fork**: `~/Claude/energy/powersync/` → `Artic0din/PowerSync`
- **Upstream**: `bolagnaise/PowerSync` (v2.12.42)
- **Daily sync**: `sync-upstream.yml` runs at 06:00 AEST

## What was done this session

### Phases completed (merged to fork main)
| Phase | PR | What |
|-------|-----|------|
| Phase 1 | #45 | Savings dashboard, decision logging, notifications, CI rewrite |
| Phase 2 | #50 | Dashboard stability, config validation, force discharge, EV stats |
| Phase 4 | #51 | Max SOC limit, forecast accuracy, auto-calibration |

### Phases 3, 5, 6, 7
- **Phase 3**: SKIPPED — weather automations already upstream, CT too niche
- **Phase 5**: PR #57 open on fork (multi-vehicle EV queuing) — has unresolved CodeRabbit findings in `ev_charging_planner.py` (asyncio.Lock, demand calc, queue eligibility). Core queue logic needs verification — worker agent may not have committed to the correct `evaluate()` method.
- **Phase 6**: Not started — trimmed to degradation-aware LP only
- **Phase 7**: Not started — mostly upstream already

### CI/Templates
- Removed 8 fork-only workflows that caused noise on upstream PRs (#84)
- Added bug report + feature request issue templates (#107)
- Updated PR template to GridWise format (#108)
- CodeRabbit now reviews drafts (`auto_review.drafts: true`)

### Issues
- Split 5 mega-issues (#26, #27, #33, #35, #38) into 22 individual issues (#85-#106)
- Created 3 new EV issues from live testing (#109-#111)
- All 26 open issues labeled and formatted with template fields
- 5 issues closed as fixed upstream (#58, #59, #61, #64, #65)

## Immediate next actions

### 1. Fix unresolved CodeRabbit threads (PRs #113, #114)
PR #113 has 1 unresolved, PR #114 has 3 unresolved. Read the comments, fix, reply, resolve, re-review.

### 2. Merge the 3 EV fix PRs (#112, #113, #114)
After CodeRabbit is clean. These fix real bugs Ryan observed:
- #112: Ghost sessions with 0 kWh never cleaned up (Fixes #111)
- #113: BLE label shows raw prefix (Fixes #110)
- #114: Mobile Connector EV invisible in energy flow (Fixes #109)

### 3. Submit bug fixes to upstream
7 PRs labeled `upstream-candidate` ready to submit to `bolagnaise/PowerSync`:

| Fork PR | Fix | Files |
|---------|-----|-------|
| #74 | Sungrow `self._model` + GoodWe zero-PV | `inverters/goodwe.py`, `inverters/sungrow.py` |
| #75 | Amber hardcoded AEST timezone | `tariff_converter.py`, `tariff_utils.py` |
| #76 | GoodWe force_charge always returns True | `inverters/goodwe_battery.py`, `optimization/coordinator.py` |
| #77 | Amber WebSocket timeout 60→90s | `websocket_client.py` |
| #78 | Load forecast sensor kWh unit + attribute size | `sensor.py` |
| #79 | tariff_rate 403 cooldown | `__init__.py` |
| #81 | EV notification per-vehicle cooldown | `automations/actions.py` |

To submit: create PRs on `bolagnaise/PowerSync` from these branches. Small, focused, no formatting changes — exactly what the dev asked for.

### 4. Phase 5 (#57) needs work
The `ev_charging_planner.py` changes (priority queue, asyncio.Lock, rebalance) need verification. The worker agent committed but the code may be in the wrong `evaluate()` method (there are multiple). Check `git diff main..feat/phase5-ev-queuing -- custom_components/power_sync/automations/ev_charging_planner.py` on the remote branch.

## Open PRs summary

| PR | Branch | Status | Threads |
|----|--------|--------|---------|
| #114 | fix/ev-flow-auto-discover | CodeRabbit reviewed | 3 unresolved |
| #113 | fix/ev-flow-label | CodeRabbit reviewed | 1 unresolved |
| #112 | fix/ev-stale-session-cleanup | CodeRabbit clean | 0 unresolved |
| #81 | fix/ev-notification-debounce | CodeRabbit clean | 0 unresolved |
| #79 | fix/tariff-rate-cooldown | CodeRabbit clean | 0 unresolved |
| #78 | fix/load-forecast-sensor | CodeRabbit clean | 0 unresolved |
| #77 | fix/websocket-timeout | CodeRabbit clean | 0 unresolved |
| #76 | fix/force-discharge-reliability | CodeRabbit clean | 0 unresolved |
| #75 | fix/amber-timezone | CodeRabbit clean | 0 unresolved |
| #74 | fix/inverter-bugs | CodeRabbit clean | 0 unresolved |
| #57 | feat/phase5-ev-queuing | Needs work | 0 unresolved but code incomplete |

## Key learnings saved to memory
- `feedback_resolve_pr_conversations.md` — MUST resolve threads via GraphQL, always re-review after fixes
- `reference_powersync_repo.md` — repo locations, upstream workflow, phase status
- `project_powersync_config_flow_ux.md` — config flow UX rewrite notes

## Upstream dev feedback
PR #53 was rejected by `bolagnaise` — too large, bundled unrelated changes, ruff format noise. Lesson: submit small focused PRs (1-2 files), no formatting changes mixed in. Config flow UX was already merged upstream via PR #27.
