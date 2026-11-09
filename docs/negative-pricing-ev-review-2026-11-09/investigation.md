# Negative export pricing and solar EV charging

Review date: 2026-11-09. Ticket: [160](https://discord.com/channels/1443943629130567874/1569223584574873673).
Initial current-source baseline: `7b8f931ff71e4cfb3d159b35db2e9f82f6122236`, published version 2.12.1668.

## Evidence boundaries

The supplied log's actual Home Assistant records run from 03:02:19.070 to
14:26:25.505 on November 9. Forecast timestamps embedded elsewhere in the log
must not extend that window. The screenshots show approximately 15:35–15:39,
outside the capture. The reporter subsequently withdrew the mismatched capture
and said they would report a recurrence. Update-check messages identify the
installed integration as 2.12.1661.

At 14:16 the log records export earnings of −0.77 c/kWh and an explicit
Sigenergy export-limit restoration for solar-surplus EV headroom. The same
exception retains normal export at 14:21 and 14:26. The charger reports
connected, without charging, and EV SOC is unknown. The stationary battery's
100% SOC is not the EV's SOC.

The prior investigation established the restoration command, acknowledgement,
readback, and subsequent export within the earlier log window. It did not
establish the later screenshot's cause. This investigation issues no customer
commands and makes no installation or post-fix physical-effect claim.

## Contract and competing explanations

Native Sigenergy and Sungrow curtailment limits grid export while permitting
solar to serve local loads. Their existing EV exception intentionally lifts that
limit to reveal spare PV so grid-based EV control can start and ramp. Removing
the exception alone risks restoring the startup/ramping problem covered by
ticket 38.

Two explanations must remain separate: an eligible EV can legitimately need a
temporary opportunity to discover spare PV; an active but non-consuming session
can retain the same exception without consuming that PV. Later screenshot-time
EV eligibility remains unknown.

Negative import pricing is distinct from negative export earnings. Solar Surplus
uses available solar and its configured export opportunity-cost ceiling; a
negative import rate does not itself authorize that mode to import for charging.
The existing price/configuration regressions passed (103 tests).

## Reproduced current-code findings

- Both EV and curtailment cached-status readers overwrite native
  `is_curtailed=True` when the separate AC inverter is normal. That hides native
  zero-export operation from the EV's existing full-battery surplus/probe logic.
  Regressions reproduce the incorrect final status through both real readers.
- The headroom helper checks activity, pause, mode and enabled state before
  awaiting plug/SOC reads. A stopped or replaced session, disabled Solar Surplus,
  or replaced entry during either read can still return permission to lift the
  native export limit. Regressions stage those changes inside the actual awaits.

- An active, plugged-in but non-consuming session can retain the native export
  exception indefinitely. The log exercises this path; current source has no
  deadline or requirement for an EV command or increased consumption.
- The idle full-battery probe inserts a minimum charging load into calculated
  surplus even when measured surplus is zero. Its eligibility also accepts NaN
  SOC, missing power data and battery discharge. Focused regressions establish
  these reachable current-code defects independently of historical attribution.

Classification: **code-confirmed**. The later screenshot remains unattributed.

## Review and repair

ChatGPT Pro reviewed the supplied current-code excerpts and a follow-up covering
the proposed safety contract. Both prompts and full responses are saved locally
in the ignored review artifact `pro-project-approach-review-guidance.txt`. Review link:
https://chatgpt.com/c/6af180fe-a818-83ec-81ea-842d20b4d908.
The independent technical audit was limited to EV admission and integration
safety; classification, changes and release remain owned by this investigation.

Native Sigenergy/Sungrow discovery now uses one non-renewable site permission
requested by the EV decision loop. It requires recent native zero-export
readback, a full home battery, safe measured power and the exact eligible EV
owner. Pending restoration and the shared cooldown are saved before opening.
Startup allows the configured sustained-surplus delay plus 150 seconds, with a
minimum 180-second and maximum one-hour permission; requests needing longer
fail closed. Ramping permits 120 seconds. The cooldown is at least 15 minutes
and at least six times the permission duration. Polling cannot extend it.

Fresh observations after the command must establish increased accepted EV load.
Acceptance closes the export exception while retaining charging; unsafe evidence
or expiry requests existing EV cleanup and native export-limit reconciliation.
An independent timer revokes permission without waiting for another price tick.
Failed native readback or uncertain cleanup stays pending; reload reconciles
that record instead of resuming discovery. The existing controller ownership,
monitoring policy and EV recovery journals remain authoritative.

Expiry reconciles native export before waiting for EV cleanup. While cleanup
waits for the existing EV command/ownership lock, the discovery lock is released
with permission revoked and new admission blocked. A forced interleaving proves
the EV decision can unwind, the stop runs in command order, and pending recovery
is retained until cleanup completes.

Both live-status readers preserve native curtailment when a separate AC inverter
is normal. The helper revalidates entry, EV state, mode, enabled state and owner
after plug/SOC awaits. Native writers serialize their decisions and preserve
the original entry boundary across those awaits.

Controllers that previously needed an idle load-following probe retain a
separate 90-second trial, a shared 15-minute backoff and immediate rejection of
missing/stale data, grid import or battery discharge. This also preserves manual
Solar Surplus bootstrap when automatic Solar Surplus is disabled. Trial power
is never reported as measured available surplus. Normal price signs, exact-zero
threshold and import-pricing policy are unchanged.

## Verification boundaries

The regressions include real EV-loop/native-handler bootstrap and ramp with
strong PV, the default two-minute delay, weak PV below minimum charge current,
idle/unknown SOC, expiry during a write, replaced owners/entries, persistence
failures, fresh register readback, failed closure/reload and shared EV budgets.
These prove software decisions and cleanup routing. They do not prove inverter
settling, installation, or physical closure while Home Assistant or the network
is unavailable. Publication requires the tracked full-suite release gate.

Focused and adjacent validation passed 1,250 tests. The final discovery/readback,
lock-order and real EV-loop/native-handler package passed 28 tests after the
last safety adjustment. The fetched intervening changes were a separate support
registration repair and release metadata; rebasing required no conflict or
change to the EV implementation.

The audited four-worker local full suite passed 12,559 tests with zero skips.
A later current-main solar-accounting addition merged cleanly; 161 focused
post-merge checks passed, including its shared live-status and source-accounting
paths. The final combined content is subject to fresh protected-main full-suite
evidence before publication.
