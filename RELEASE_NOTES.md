<!-- release: v2.12.1720 -->

## Sigenergy grid status

Sigenergy systems connected directly through Modbus TCP now report on-grid or off-grid status in Grid Status, with a new Sigenergy Off-Grid binary sensor for dashboards and automations. Both automatic and manual off-grid operation are recognized, using PowerSync's existing connection.

Grid Status also shows the reported mode and gateway/meter connectivity as separate attributes. Unavailable or unsupported readings show unknown rather than keeping an old grid state. Update through HACS and restart Home Assistant.

## Clearer battery controls

Charge and Discharge now come first on the dashboard. Exact battery power, Idle and battery-level targets are grouped under Advanced battery controls. Site limits and backup preparation have clear, full-width Show/Hide sections instead of small disclosure arrows.

Active battery targets keep their Stop button visible when the advanced form is closed. Backup status and recovery actions remain visible too. Existing command confirmation and gateway limits still apply.

Update through HACS and restart Home Assistant.
