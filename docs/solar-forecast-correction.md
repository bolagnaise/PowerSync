# Live solar forecast correction

## Plan

Make the existing live-production correction an explicit Solar setting, disabled
by default for both new installations and upgrades. Preserve the existing
correction when the user enables it, and clear its retained factor when disabled.
Expose the preference through Home Assistant and the mobile Solar Forecast
settings, including persistence and compatibility with older clients/backends.

Validate the default, opt-in, opt-out, retained-factor reset, settings round trip,
and unchanged deadline safeguards before delivery.

## Setting and behavior

**Live solar forecast correction** is in PowerSync's **Weather & solar forecast**
options and the mobile app's **Solar Forecast** settings. The saved boolean is
`solar_nowcast_enabled`; a missing preference means **off**, including after an
upgrade. There is no migration that enables it for existing users.

When off, PowerSync does not lower the solar forecast in response to live
production readings. It also clears any correction retained from an earlier
cycle. Forecast-provider selection, the selected Solcast estimate, curtailment,
reserve constraints, and Charge By Time safety allowances continue to apply.

When enabled, live production below 75% of the first forecast slots' average can
reduce the forecast used for battery planning. The adjustment tapers back to the
provider forecast over the next six hours and recovers gradually as production
improves. This can bring grid charging forward. Existing near-full-battery,
telemetry-validity and solar-stop safeguards still apply.

The Solar Forecast sensor exposes `solar_nowcast_enabled` and
`solar_nowcast_derate` alongside its forecast arrays. A factor of `1.0` means no
live correction. This setting does not change the provider's own forecast.

The weather settings API advertises `solar_nowcast_supported: true`. New mobile
clients show and submit the switch only when that capability is present. A save
that omits the preference preserves its current value; explicit changes require
a boolean and reload the integration.

## Verification

Regression coverage exercises default-off upgrades, explicit opt-in, disable and
re-enable, stale-factor reset, settings persistence and validation, mobile screen
load/save, older-backend compatibility, and the existing solar learning and
deadline constraint tests. These are code checks, not installation or physical
battery verification.

Results:

- 981 focused and adjacent Python tests passed across settings, coordinator,
  learning, forecasts, battery export, reserve, SOC-cap and spread regressions.
- Mobile screen/adapter tests, forecast curtailment tests and the full
  `tsc --noEmit` check passed.
- Changed Python modules compile; English form labels/help match; whitespace
  checks pass in all three repositories.
- Two unrelated source assertions also fail on untouched baseline
  `a5e3934e`: `test_powerwall_smart_optimization_hides_spread_options` and
  `test_api_contract_is_schedule_aligned_and_additive`. They were reproduced in
  a separate baseline worktree and are outside this feature's passing test sets.

The implementation is on `codex/solar-nowcast-setting` in the HA, mobile and wiki
repositories. No release version, store build or installation is included.
