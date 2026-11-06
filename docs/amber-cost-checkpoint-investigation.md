# Amber cost totals reverting: 7 November 2026 intake

Outcome: **code-confirmed** persistence defect; customer incident attribution
remains unverified pending logs.

## Report and scope

The supplied Discord screenshot reports Today so far and month-to-date costs
moving backwards, and a previous-day difference between Amber and PowerSync.
A second reporter mentions changes across reboots. Installed versions, battery
brands, restart type and timestamped sensor states are not available.

The investigation uses fetched main at `e487a9c7` (manifest 2.12.1643;
2.12.1642 was the latest published release at investigation start). No customer
system was accessed or controlled.

## Confirmed current defect

`EnergyAccumulator.update()` requests a save after each integrated power sample.
`_schedule_save()` previously called `Store.async_delay_save()` every time, with
a 300-second delay. Home Assistant's delayed save is a debounce: each call moves
the write deadline. With ordinary telemetry arriving more frequently than five
minutes, the accumulator could run indefinitely without a disk checkpoint.

The virtual-clock regression polls every 30 seconds for 20 minutes. Before the
fix it produces zero writes, both for positive export earnings and negative
import prices. A restart without final writes would therefore restore the old
disk snapshot, losing both daily and monthly energy and cost progress. Normal
integration unload and Home Assistant's final-write handler can still save
successfully; this test does not claim every graceful reboot loses data.

The fix leaves one delayed save pending and reads the latest totals when that
deadline fires. The next sample schedules the following checkpoint. Explicit
flushes release the pending marker so subsequent updates continue saving.

The HA contract was verified against its official source:
https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/helpers/storage.py
(`async_delay_save` and `_async_schedule_callback_delayed_write`).

## Other explanations and limits

- The Today so far card uses PowerSync's live measured estimates, with Amber
  metered cost shown separately. Estimates are not guaranteed to equal Amber's
  later billing data. Missing power samples and downtime cannot be reconstructed
  from a saved accumulator snapshot.
- Amber usage aggregation replaces each returned day's aggregate, subject to
  its quality guard. A shorter successful API response could shrink a total,
  but no raw response establishes that Amber returned incomplete coverage here.
  Do not impose monotonic monetary totals: negative prices and legitimate
  billing corrections can move totals in either direction.
- The checkpoint repair does not reconstruct already lost historical data,
  explain a rollback without a restart, or prove the screenshot's exact dollar
  difference. An abrupt restart can still lose the latest checkpoint window.

For customer attribution, collect logs spanning the change/reboot, the restart
type, installed version and battery brand, and the daily import/export/net-cost,
month-to-date average and Amber metered sensor states with attributes immediately
before and after. For a change during an Amber refresh, capture the returned
interval dates, channels and coverage in a sanitized diagnostic artifact.

## Validation

The virtual-clock regression cases fail on the prior source (zero checkpoints)
and pass with the fix. A further test uses the existing verbatim Home Assistant
Store fixture and a real JSON file to verify checkpoints and restored earnings.
Focused and adjacent energy, daily-cost, Amber usage, optimizer refresh and
storage-cadence tests pass: **109 passed**. Release additionally requires the
audited full suite and matching trusted main CI evidence under
`docs/regression-release-gate.md`.
