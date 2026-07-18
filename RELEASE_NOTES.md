<!-- release: v2.12.886 -->

## What's Changed

**Recover existing safe CovaU quota baselines after updating**

PowerSync now re-evaluates an existing CovaU SolarMax daily quota baseline when
it was saved by an older release before the first eligible import or export
window. Updating and reloading can therefore restore authoritative quota
tracking and the 11:00–14:00 free-import forecast immediately, without waiting
for the next midnight reset.

Recovery remains disabled when the saved sample was taken after an eligible
window began, or when a meter reset, correction, telemetry gap, or estimated
power source made the daily balance uncertain.

Update available via HACS
