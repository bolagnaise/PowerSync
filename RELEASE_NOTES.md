<!-- release: v2.12.508 -->

## What's Changed

**Powerwall solar string voltage sensors**
PowerSync can now poll paired Tesla Powerwall systems for DC solar string diagnostics and create per-string voltage sensors in Home Assistant. PW3 component telemetry and legacy PVAC/PVS payloads are both normalized, with string metadata such as MPPT label, current, power, connection state, source, and grouped MPPT totals exposed as attributes when available.

**Self Consumption dashboard action**
The generated Home Assistant dashboard now includes a dedicated Self Consumption control beside the existing force charge, force discharge, hold SoC, and restore controls. This gives Sigenergy and other supported battery users a direct instant-mode button for the existing self-consumption service.

Update available via HACS
