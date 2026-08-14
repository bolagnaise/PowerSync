<!-- release: v2.12.1103 -->

## What's Changed

**Detect Tesla Powerwall calibration from the gateway alert**

PowerSync now treats the local `BatteryCalibration` alert as the authoritative calibration signal instead of waiting for repeated operation-mode failures. Home Assistant and mobile notifications identify the active calibration, the Calibration Active entity records whether the evidence came from the gateway or the legacy mode-stick fallback, and the optimiser continues planning while automatic charge/export command execution is guarded. Normal control resumes after three consecutive clean two-second snapshots, while notification delivery runs separately so it cannot delay local telemetry polling.

**Wire every local V1R status path through to Home Assistant and the dashboard**

The Powerwall Local Control card now resolves its binary sensors and grid-mode switches correctly and includes the calibration state. Active alert details, island state, Powerwall count, pack energy, battery blocks, and per-endpoint V1R diagnostics are exposed through entities and the authenticated local-status API. Device, firmware, network, and internet requests now fail independently, so an unsupported identity response no longer hides valid network or internet status.

**Reject stale or invalid local-control evidence safely**

Expired local snapshots no longer report the gateway as available, revoked or inactive RSA keys now activate the re-pair workflow, and replaced coordinators release their listeners and background diagnostic tasks. Direct-LAN and Fleet-relay V1R requests also use separate signature lifetimes, preventing the longer cloud lifetime from being sent to the local gateway.

Update available via HACS
