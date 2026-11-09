# Additional solar accounting

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

## Avoid counting the same generation twice

Leave additional solar disabled if the primary reading already includes the
array. Existing Sungrow AC-inverter, FoxESS CT2 and GoodWe whole-site solar
handling continue to work with this option disabled. A GoodWe whole-site override
cannot be combined with another additive source. Runtime guards also refuse
known already-inclusive Sungrow/FoxESS paths and primary-source identity
conflicts; the selected sensor and explicit topology declaration still need to
describe the actual installation.

## Missing readings and totals

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
