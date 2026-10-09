<!-- release: v2.12.1364 -->

## What's Changed

**Keep overlapping GoodWe curtailment checks in order**
Automatic price updates and periodic checks now complete one GoodWe curtailment
or restore transition at a time. Previously, callbacks arriving during an
inverter write could send duplicate commands, skip a required opposite-direction
transition, or let an older restore overwrite the status of a newer curtailment.
Each waiting callback now evaluates the completed transition before deciding
whether another command is needed.

**Preserve Pending and retry backoff after an interrupted write**
GoodWe export-limit writes enter Pending before awaiting the inverter. If the
operation is interrupted, Pending and the recorded retry backoff remain, the
status is refreshed, and the next callback does not immediately replay the
command.

This fixes automatic command ordering. It does not establish the cause of an
ESA continuing to export after an accepted zero-watt limit. Active still
requires fresh telemetry within the 250 W export tolerance; GoodWe Off-grid
mode is not added as a curtailment fallback.

Update available via HACS
