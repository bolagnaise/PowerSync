## What's Changed

**UK Octopus users now get import + export price sensors and a sensible default for SEG**
The Octopus Energy provider previously didn't register price sensors at all — only Amber and Localvolts users got them. Octopus now wires up the same price-sensor entities (current import, feed-in, forecast) from `OctopusPriceCoordinator`. For users whose Octopus Energy integration only exposes an import tariff (common for SEG households without a specific export agreement), the coordinator now synthesises a 4.1 p/kWh feed-in entry tagged `synthetic_seg`, so the export price shows as a sensible negative-cost value instead of being blank.

**Octopus config changes from the OptionsFlow take effect immediately**
The Octopus product code, tariff code, region and export tariff settings were being read from `entry.data` only, so any change you made in the OptionsFlow needed a reload to be picked up. They now use an entry-value helper that reads from `entry.options` first, so updates apply on the next coordinator refresh. The Amber-token detection has also been tightened: previously a stale Amber API token left over from an earlier provider could still spin up an `AmberPriceCoordinator` and cause a 403/`ConfigEntryNotReady` retry loop on Octopus, GloBird, AEMO VPP, NZ and "other" setups; the token is now only honoured when the active provider is genuinely Amber (or Flow Power configured to use Amber as its price source).

**Solar-surplus EV charging now actually rides through the configured grace period**
When solar surplus dipped below the minimum needed to charge, the dynamic EV controller was supposed to hold the current charge rate for `stop_delay_minutes` before reducing or stopping — giving brief cloud cover or load spikes a chance to clear. On the very first low-surplus sample it would set the timer to "now" but then fall through with the recalculated `new_amps` (often near zero), so the EV would immediately drop or stop instead of holding. The first low-surplus sample now also clamps `new_amps` back to `current_amps`, matching the behaviour of subsequent samples within the grace window. Added unit tests covering both the hold-on-first-sample and stop-after-delay paths.

Update available via HACS
