<!-- release: v2.12.791 -->

## What's Changed

**Flow Power KWatch outage fallback**

- Added automatic runtime fallback from Flow Power KWatch pricing to AEMO Direct pricing when KWatch has a transient outage, timeout, invalid response, or empty dispatch/forecast payload.
- Kept KWatch as the configured and primary price source. The fallback is internal to the KWatch coordinator and does not change the Flow Power PEA formula, account summary behavior, BPEA, GST, TWAP, NMI, or site metadata.
- Exposed fallback status through Flow Power price sensor attributes and generated tariff schedule metadata, including the effective price source, fallback state, fallback reason, and last successful KWatch refresh where available.
- Preserved KWatch authentication validation: explicit `invalid_api_key` responses remain configuration/auth errors and do not silently fall back to AEMO.

**Battery control and telemetry fixes**

- AlphaESS curtailment now skips while a force command or optimizer dispatch is active, avoiding conflicting export-limit actions.
- Home load telemetry now keeps EV charging and battery-charge power out of the derived home-load value.
- Sungrow same-endpoint AC curtailment protection now applies across models, not only SH systems.

**Optimizer reserve-floor fix**

- Smart Optimization now lets the per-slot reserve floor win over the cross-run scalar reserve when building the optimization plan.

Update available via HACS.
