<!-- release: v2.12.1191 -->

## What's Changed

**GoodWe curtailment status now requires physical export proof**
For direct GoodWe control, DC Solar Curtailment now remains Pending after an acknowledged zero-export limit until fresh inverter telemetry confirms residual grid export is at or below 250 W. This prevents the card from describing register readback alone as “Export confirmed stopped.” Unsupported entity-only profiles, failed commands, stale telemetry, and force-dispatch ownership remain fail-closed as Pending.

**Immediate GoodWe curtailment status refreshes**
GoodWe curtailment, restore, unsupported-profile, and failed-command lifecycle changes now refresh the card immediately. A failed restore remains Pending rather than reporting a normal export state.

Update available via HACS
