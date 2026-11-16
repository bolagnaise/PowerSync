<!-- release: v2.12.1720 -->

## Sigenergy grid status

Sigenergy systems connected directly through Modbus TCP now report on-grid or off-grid status in Grid Status, with a new Sigenergy Off-Grid binary sensor for dashboards and automations. Both automatic and manual off-grid operation are recognized, using PowerSync's existing connection.

Grid Status also shows the reported mode and gateway/meter connectivity as separate attributes. Unavailable or unsupported readings show unknown rather than keeping an old grid state. Update through HACS and restart Home Assistant.
