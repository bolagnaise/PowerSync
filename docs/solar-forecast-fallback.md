# Solar forecast fallback

PowerSync already tries a second provider when the selected provider has no
usable forecast. Extend that selection to handle Solcast's daily API quota,
including when a cached Solcast forecast still covers the planning window.

## Selection rules

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

## Status and compatibility

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

## Setup

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

## Verification

Regression coverage includes external and built-in quota exhaustion,
automatic reset/recovery, a new forecaster after restart, missing or expired
Open-Meteo data, valid zero forecasts, malformed quota data, and existing primary
provider preferences. Optimizer diagnostics and notices are checked independently
of the forecast values. Publication follows `docs/release-procedure.md` and the
trusted full-suite regression gate. Installation and live Home Assistant checks
remain separate from publication.
