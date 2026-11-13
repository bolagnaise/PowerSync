# Solar accounting, forecasts and curtailment

- [Additional solar accounting](#additional-solar-accounting)
- [Stop solar during negative import prices](#negative-import-solar)
- [Live solar forecast correction](#solar-forecast-correction)
- [Solar forecast fallback](#solar-forecast-fallback)
- [Fronius simple-mode curtailment after restart](#fronius-simple-mode-restart)

<a id="additional-solar-accounting"></a>

## Additional solar accounting

Use **Configure → Advanced settings → Additional solar accounting** when a
separate AC-coupled array is missing from the primary Solar reading. For example,
a GoodWe inverter may expose its own PV power sensor while the battery inverter
reports only the panels connected directly to it.

Enable **Include additional solar generation** and select the separate array's
registered Home Assistant power sensor. The sensor must report W, kW or MW.
The saved declaration binds to its integration, device and unique identity.
Renaming the same registered entity preserves that binding; replacing the source
requires selecting it again in settings.

| Setting | Purpose |
| --- | --- |
| Include additional solar generation | Declare that this array is missing from the primary Solar reading. |
| Additional solar power sensor | Select the separate array's registered power sensor. |
| Home Load source | Preserve measured consumption or rebuild a load derived from incomplete solar. |

Choose **Keep measured Home Load** if the primary meter already measures all
household consumption. Choose **Calculate Home Load from site power balance**
only when the original load was derived from solar that excludes this array and
the grid meter measures the whole property. PowerSync then uses total solar plus
grid import plus battery discharge, with charging/export represented as negative
values. Existing EV attribution excludes charging from Home Load once.

The setting changes accounting only. It does not enable curtailment, make a
control request, or change the inverter's configuration. It reads the selected
Home Assistant sensor without connecting another inverter controller.

### Avoid counting the same generation twice

Leave additional solar disabled if the primary reading already includes the
array. Existing Sungrow AC-inverter, FoxESS CT2 and GoodWe whole-site solar
handling continue to work with this option disabled. A GoodWe whole-site override
cannot be combined with another additive source. Runtime guards also refuse
known already-inclusive Sungrow/FoxESS paths and primary-source identity
conflicts; the selected sensor and explicit topology declaration still need to
describe the actual installation.

### Missing readings and totals

Solar is composed before energy accumulation and publication. HA sensors,
dashboard, mobile and optimiser consumers receive that same canonical total.
When paired, Tesla uses a fresh local primary sample before its cloud fallback.
Source changes break solar integration continuity rather than applying the new
reading to an interval measured under the previous source.

An unavailable, restored, invalid or more than two-minute-old source makes total
Solar unavailable. A fresh measured zero remains valid. Observation time comes
from the HA source state, not the time PowerSync reads the cache. Independently
valid grid, battery, measured load and billing accounting continue. A load derived
from the missing solar is withheld too. The first valid sample after a gap starts
a new solar interval; missing production is not filled retrospectively.

Existing accepted daily and month totals are retained across activation, reload,
disable and source changes. Additional generation is counted prospectively;
inverter-only daily counters cannot overwrite the combined sampled total. The
Solar attributes and energy summary describe partial coverage. Normal midnight
and month boundaries still reset their respective counters. Existing provider
lifetime counters retain their original scope; earlier AC generation is not
reconstructed or added to them.

The feature is off by default. Existing installations retain their previous
solar accounting until explicitly configured.

<a id="negative-import-solar"></a>

## Stop solar during negative import prices

Available from PowerSync v2.12.1418 for Home Assistant. The policy is
**off by default**. It stops PV generation when the customer's current retail
import price is below zero, allowing existing household loads and permitted
battery charging to use paid grid imports. Zero export is a different operation:
it can leave solar supplying the house and battery.

Enable **Stop solar when the import price is negative** in PowerSync's
**Curtailment settings**. Existing battery dispatch, grid charging permissions,
SOC limits, electrical limits and EV settings continue to determine consumption.
The feature does not create extra loads or override those limits. Choosing to
stop solar can forgo export income; this is an explicit import-price policy,
not a calculation that selling solar is always less valuable.

### Configuration fields

| Setting | Use |
| --- | --- |
| Stop solar when the import price is negative | Opt in to stopping generation below zero retail import price. Off by default. |
| PV production controls (optional with supported native inverter) | Select verified PV-only switches or production-limit numbers, or leave empty for a supported native control. |
| These controls cover all solar generation | Enable only when every PV source is controlled, allowing the optimiser to plan without solar in negative-price intervals. |
| Controlled solar production power sensor (optional) | Select power from the PV sources being controlled so fresh readings can confirm a production stop. |
| Retail import price sensor (optional) | Supply a timestamped retail import interval when the provider cannot supply one directly. |
| Grid connected sensor (optional for Tesla) | Required for non-Tesla systems. The selected sensor must provide fresh proof of utility-grid connection. |

### Controls and inverter coverage

Explicitly selected Home Assistant **PV production controls** work for any brand
whose integration exposes a suitable control. Multiple controls can cover
multiple inverters. A switch must mean **on = PV enabled, off = PV stopped**;
a number must limit **PV generation**, accept zero, and have readable limits.
Do not select an export limit, grid/islanding switch, battery dispatch setting,
whole-hybrid shutdown switch, or a general load relay.

With no production entities selected, the following native controls are used:

| Adapter | Production operation | Conditions and restore |
| --- | --- | --- |
| Enphase | DPEL with `enable=true`, `export_limit=false`, `limit_value_W=0`, dynamic limiting enabled | Requires readable modern DPEL settings and write access. Refreshes every 15 seconds. Restores the captured control fields, preserving fresh installer metadata. No DER, profile, opposite-export-flag or disabled-limiting fallback. |
| Fronius | SunSpec active output limit at 0% | Separate PV inverter only. Saves enable, limit and reversion settings; does not introduce a reversion-to-100% timeout that could overwrite a prior cap. Fronius GEN24 storage entries require an independently verified PV-only entity instead. |
| Sungrow SG | Run-mode register `5005=206` | Explicitly known standalone SG model and readable run-mode baseline required. Restores the saved run mode; never stops an SH hybrid or changes its battery settings. |
| SolarEdge | Active-power limit at 0% | Separate PV topology; duplicate battery endpoint remains blocked. Restores the previous percentage, including non-default limits. |
| Zeversolar | Percentage production limit at 0% | Reads the actual advanced settings, saves enabled/mode/limit fields and restores them exactly. |
| Sigenergy | PV maximum-power limit `40036`, unsigned 32-bit zero | DC PV only. Remote EMS must already be enabled in a mode where that register is effective (3–6). Never changes EMS mode, battery dispatch, grid export limits or third-party AC solar. Restores the original PV register even after the EMS mode changes. |
| FoxESS, Huawei, GoodWe, GoodWe entity bridge, SolaX, AlphaESS | Explicit PV production entities | Their existing PowerSync curtailment adapters control export/dispatch, not an established independent PV-only stop. They are not silently promoted to production controls. |
| Other brands | Explicit PV production entities | Requires a documented PV-only control exposed by the installed integration. A device without such a surface cannot be stopped by software through this feature. |

Reading back a zero limit confirms a setting, not a physical effect. The new
**Solar Production Control** sensor reports **Stop Pending** until a fresh
controlled-PV power observation demonstrates a transition from producing over
100 W to at most 100 W. Without a suitable power sensor, or when solar was already
zero at night, physical confirmation remains unknown. Command attempted,
acknowledgement, device readback and physical convergence are separate attributes.
`restore_required` identifies an outstanding ownership record;
`restore_pending` identifies recovery currently awaiting completion.

### Price validity and recovery

A fresh utility-grid connection is required before stopping PV. Tesla uses its
own current grid status. Other systems select **Grid connected sensor**: a
binary sensor whose `on` state means utility-connected, or a sensor reporting
`Active`, `Connected`, `SystemGridConnected` or `On-Grid`. Observations older
than 90 seconds are rejected. A lost/unknown grid connection withdraws the stop
and restores owned PV settings; it never suppresses backup solar from an old
negative-price quote. Only an off-grid fallback session owned by PowerSync can
be reconnected for this policy; a manual islanding state is not taken over.

Amber, Localvolts, Octopus and EPEX use finite, timestamped **current retail import
intervals**. Feed-in prices are unnecessary for this policy. Forecast intervals,
expired intervals, failed coordinator updates, NaN/infinity and raw AEMO/KWatch
wholesale observations cannot authorize a new stop.

Other providers can supply the optional **Retail import price sensor** with
`startTime`/`endTime` or `valid_from`/`valid_to` timezone-aware attributes. Supported
units are `c/kWh`, `¢/kWh`, `p/kWh`, `$/kWh`, `AUD/kWh`, `EUR/kWh`, `GBP/kWh`,
`€/kWh`, and `£/kWh`. Currency-per-kWh values are converted to minor units.
The value must be the customer's retail buy rate. An old scalar tariff or a
sensor's change timestamp is not a current tariff interval.

At zero or a positive price, missing/expired price, disabled policy, changed
controls or unload, PowerSync restores only the settings it still owns. The
baseline is saved durably before the first write and retained after lost replies,
partial writes and failed restores. Persistent device stops can remain in place while Home Assistant is offline; recovery needs Home Assistant and device connectivity. Restart loads that obligation before legacy
AC controls can write. Failed commands use bounded exponential retry intervals.
External changes are preserved and suspend this policy for the negative-price
window. Manual inverter services restore the owned production settings first
and suspend automatic stopping until a nonnegative price is observed.

Monitoring Mode blocks commands unless the existing explicit **Allow curtailment
control in Monitoring Mode** permission is enabled. Permission transitions settle
owned recovery before relinquishing control. Remaining export curtailment is
re-evaluated after production recovery. Negative-import stopping never islands
the Powerwall; optimizer off-grid projections exclude negative import slots.

### Optimizer and evidence

Enable **These controls cover all solar generation** only when every PV source is
covered. After current device controls have been confirmed, the optimizer can
project zero PV during negative import slots. Mixed or unconfirmed sites retain
their forecast. Raw solar forecasts are preserved, and deliberate production
suppression is excluded from forecast learning and cloud nowcasting.

The capability matrix was checked against the adapter implementations at
`0863c7d2e85ca46d8ff1272cc246685c28321cca` (published v2.12.1417), then covered
by focused control, recovery and routing regressions. These are source and
simulated-control checks; they do not establish physical inverter behavior.

Primary references:

- [Enphase Envoy integration](https://www.home-assistant.io/integrations/enphase_envoy): production controls and battery limitations.
- [Enphase solar troubleshooting guide](https://enphase.com/sites/default/files/2025-08/Enphase-solar-troubleshooting-guide-ING-00061-2.0-EN-INT-2025-08-14.pdf): production disabling also stops Enphase battery charging/discharging. The native implementation uses explicit DPEL production limiting instead of the whole-system production-disable control.
- [Fronius GEN24 Modbus instructions](https://manuals.fronius.com/html/4204102649/en-US.html): active-power limits affect inverter output; battery topology matters.
- [SolaX remote settings](https://www.solaxpower.com/uploads/file/a1-ess-g2-remote-setting-user-manual-en.pdf): export power is a grid-export constraint.
- [Zeversolar ZeverManager manual](https://www.zeversolar.com/fileadmin/user_upload/pdf/usermanuals_en/zevermanager_user_manual_en.pdf): active-power percentage limitation.
- The pinned repository adapters and the focused regressions are the protocol implementation evidence; no new undocumented register addresses are introduced.

No customer inverter commands, Home Assistant installation, physical import
increase or paid-import earnings were verified during implementation.

<a id="solar-forecast-correction"></a>

## Live solar forecast correction

### Setting and behavior

**Live solar forecast correction** is in PowerSync's **Weather & solar forecast**
options. The corresponding mobile **Solar Forecast** switch is prepared for a
separate app update. The saved boolean is
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

<a id="solar-forecast-fallback"></a>

## Solar forecast fallback

PowerSync already tries a second provider when the selected provider has no
usable forecast. Extend that selection to handle Solcast's daily API quota,
including when a cached Solcast forecast still covers the planning window.

### Selection rules

- Keep the configured primary provider. This feature does not change saved
  settings or request updates from either forecast integration.
- When Solcast reports an exhausted daily quota, try the existing Open-Meteo
  fallback before using Solcast's cache. Apply this to both the installed
  Solcast Solar integration and PowerSync's built-in Solcast client.
- Reevaluate quota on every forecast read. Return to Solcast automatically when
  its quota resets; do not persist a temporary fallback as the primary provider.
- If Open-Meteo has no usable forecast, retain usable Solcast cache and explain
  that the fallback is unavailable. If neither source has usable data, preserve
  the existing zero-solar forecast and missing-forecast warning.
- Forecast periods must overlap the requested window. An expired Open-Meteo
  series must not become an apparently valid zero-solar fallback. Valid zero
  production remains usable, including at night.
- Keep explicit Open-Meteo and Volcast preferences and their existing fallback
  order. Only move quota-limited Solcast behind Open-Meteo.

### Status and compatibility

Expose the configured provider, active provider, fallback reason, and whether
Solcast's quota is exhausted in forecast diagnostics. Show a short optimizer
notice for a quota fallback or an unavailable fallback. Daily summaries and EV
solar forecasting use the same shared selector.

Quota detection uses existing local coordinator/API metadata and supported
Solcast quota sensors. Missing or invalid quota metadata is unknown, not proof
that the limit is exhausted. Do not use sensor update times as quota-reset or
forecast-generation times.

Solcast Solar's local `coordinator.solcast.api_used_count` and `api_limit`
properties describe quota usage. Its own coordinator resets the persisted
counters at UTC midnight. For older integrations, pair the `API Used` and
`API Limit` diagnostic sensors. PowerSync's built-in client resets its UTC-day
counter before returning cached forecasts or checking the next request's budget.
This also fixes a cached-return path that previously prevented new-day polling.
Ignore upstream counters when its usage-cache freshness flag says they belong
to an earlier UTC day. Older sensor-only integrations must update their quota
sensors before PowerSync can observe a reset; missing quota metadata leaves the
existing data-availability fallback in place.

Upstream references: [Solcast API properties](https://github.com/BJReplay/ha-solcast-solar/blob/main/custom_components/solcast_solar/solcastapi.py),
[Solcast quota reset](https://github.com/BJReplay/ha-solcast-solar/blob/main/custom_components/solcast_solar/sites_cache.py),
[Solcast midnight callback](https://github.com/BJReplay/ha-solcast-solar/blob/main/custom_components/solcast_solar/coordinator.py).

### Setup

Install and configure **Open-Meteo Solar Forecast** in Home Assistant for the
site's PV arrays. The ordinary Open-Meteo weather integration does not provide
the required solar-power forecast. In **PowerSync → Configure → Weather & solar
forecast**, leave **Solar forecast provider** set to **Solcast**. There is no
additional fallback toggle and no need to copy an API key into PowerSync when
using the external Solcast Solar integration.

When quota is exhausted, the optimizer notice names Open-Meteo as the active
source. Its API exposes `solar_forecast_status`, also available as
`forecast_status` on the LP solar forecast sensor. The fields are
`preferred_provider`, `active_provider`, `fallback_reason`, and
`solcast_quota_exhausted`. Selection takes effect on the next normal forecast
read or optimizer cycle; this feature does not force a refresh or consume calls.

<a id="fronius-simple-mode-restart"></a>

## Fronius simple-mode curtailment after restart

PowerSync retains a restore record before it disables SunSpec throttling in
Fronius simple mode. After a Home Assistant restart, it checks that record against
the configured endpoint, the inverter's SunSpec serial number and fresh control
registers. A matching curtailment is shown as pending until physical convergence
is verified. Restoration still follows the existing price, battery, monitoring
and manual-control rules; startup recovery itself sends no control command.

This covers changes made from normal output (SunSpec limiting enabled at 100%).
Disabled throttling by itself does not prove that PowerSync owns the setting.
Pre-upgrade states without a restore record, changed devices, and user power caps
are therefore not automatically adopted. Failed restores retain the record for
retry. Neither deleting the record nor an unconditional startup restore is a safe
recovery procedure.

The record uses the SunSpec Model 1 serial layout, documented in the
[SunSpec common model](https://github.com/sunspec/models/blob/master/json/model_1.json).
An unavailable serial or mismatching readback defers recovery.

Low SOC alone does not override an active limit while export remains uneconomic.
Fresh battery charging with headroom remains an exception. Negative import prices
retain priority. To investigate a different outcome, capture the same-time prices,
battery power and SOC, grid power, inverter status attributes, and untruncated
PowerSync debug records around the transition.
