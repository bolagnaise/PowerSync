<!-- release: v2.12.1322 -->

## What's Changed

**Tesla BLE: an explicit Stop is no longer discarded before it is sent**
After a failed Bluetooth wake, PowerSync stopped attempting further wakes for
60 seconds so the automatic Solar Surplus loop would not spend a full wake
timeout on every 10-second tick. That pause was shared by every Tesla BLE
control path, including a Stop you press yourself, and it returned without
sending the command and without writing anything to the log. On an unhealthy
BLE bridge the automatic loop re-armed the pause roughly every 100 seconds, so
for most of the time any requested stop was dropped before dispatch, invisibly.

An explicitly user-initiated Stop now always attempts the wake, and a
suppressed automatic command is reported in the log (rate-limited, so a
repeating loop cannot flood it) together with an explicit
"stop not dispatched" line on the stop path. The wake requirement itself, the
2-minute telemetry freshness rule and the command readback confirmation are
unchanged: a stop is still only reported as successful when the vehicle
confirms it.

This corrects a reproduced code defect in the command path. It does not repair
a BLE bridge that has stopped publishing telemetry — if your `tesla_ble`
entities are minutes or hours stale, the proxy itself still needs attention.

**FoxESS: negative-price curtailment no longer waits for a full battery**
While an optimizer or manual force charge was in flight, PowerSync skipped
solar curtailment so it would not cancel a paid charge. But a FoxESS force
charge is written in AC-output mode, which does not hold grid feed-in, and with
the battery already at 100% it also absorbs nothing. The result was a site
exporting at a negative feed-in price for the whole charge window while
curtailment was available and economically warranted.

A force charge now only holds that ownership while the battery still has charge
headroom. With no headroom, curtailment proceeds and grid export is held at
zero. A productive charge with real headroom still wins, force discharge and
export windows are unchanged, and unreadable or stale battery telemetry keeps
the existing behaviour.

**Sungrow: curtailment could be switched off silently and then stay stuck**
When PowerSync applied its zero-export curtailment limit and the optimizer
afterwards returned the inverter to self-consumption, that restore disabled the
curtailment export limit and consumed its ownership record. The curtailment
state still read "curtailed", so the next negative-price check saw nothing to
do and the price-recovery restore could not run — leaving the dashboard showing
curtailment Active indefinitely while the inverter was exporting normally.

Restoring normal operation now preserves an active curtailment export limit
(matching the existing Sigenergy behaviour), the periodic check re-applies the
limit whenever PowerSync no longer owns it, and a failed restore reports
"Pending" instead of latching on "Active". Optimizer-owned export limits are
still restored exactly as before.

Focused regressions cover all three: a user stop under an armed wake backoff, a
suppressed automatic stop, force charge at 100% and at 60% SOC, unreadable and
stale battery telemetry, a preserved versus an optimizer-owned Sungrow export
limit, re-apply after ownership loss, and a failed restore.

Update through HACS and restart Home Assistant. If you reported any of these,
please retest on this version and say what you see.

Update available via HACS
