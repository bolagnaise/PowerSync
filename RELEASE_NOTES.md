<!-- release: v2.12.753 -->

## What's Changed

**Sungrow SH negative feed-in curtailment**
PowerSync now applies Sungrow SH native curtailment as a zero grid-export limit during negative feed-in periods, letting the inverter load-follow internally instead of using the current home load as the export-limit value. This prevents full batteries from continuing to export excess solar when the tariff is charging for exports.

**Curtailment after restart**
PowerSync now runs an initial solar-curtailment check shortly after startup, instead of waiting for the next five-minute curtailment tick. That closes the restart window where a full battery could keep exporting during a negative feed-in period before the scheduled check fired.

Update available via HACS
