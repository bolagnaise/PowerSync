<!-- release: v2.12.1219 -->

## What's Changed

**AC solar curtailment now holds its limit through uneconomic export periods**
When an AC inverter was already curtailed, its successful limit could make site export appear to stop. PowerSync no longer treats that controlled result as a reason to restore unrestricted inverter output while the export price remains uneconomic.

The limit is still relaxed when live battery telemetry confirms material charging with capacity remaining, and it restores normally once the export-price window recovers. This prevents a restore-to-export-to-recurtail loop without changing optimizer planning or claiming a live hardware result.

Update available via HACS
