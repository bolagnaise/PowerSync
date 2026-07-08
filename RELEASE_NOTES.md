<!-- release: v2.12.799 -->

## What's Changed

**GoodWe export plans stay consistent with entity telemetry**
PowerSync now performs a one-time GoodWe capability probe when Home Assistant entity telemetry is active but does not expose the inverter rated power. This keeps target-export planning aware of the inverter's physical discharge headroom, so Flow Power Happy Hour and other export windows can continue planning the configured net grid export target instead of being reduced by forecast house load after a reload.

**Entity telemetry remains the normal GoodWe runtime path**
The probe is best-effort, cached, and limited to the missing rated-power case. GoodWe TCP/LAN Kit-20 systems still use Home Assistant entity telemetry for regular runtime reads, while installs that cannot be probed directly keep the previous safe behavior.

Update available via HACS
