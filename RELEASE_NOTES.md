<!-- release: v2.12.530 -->

## What's Changed

**Monitoring mode manual controls**
PowerSync monitoring mode now keeps blocking automated optimizer and automation writes while allowing explicit user controls from Home Assistant entities and the PowerSync controls surface. Manual backup reserve, operation mode, grid export, grid charging, Storm Watch, off-grid EV reserve, VPP enrollment, force charge/discharge, hold SoC, self-consumption, and restore-normal actions can now stick when the integration is in monitoring mode.

**Automation source tracking**
PowerSync automation actions now mark their battery-control service calls as automation-sourced, so monitoring mode still prevents scheduled or internal writes from reaching the inverter or Powerwall. This preserves the monitoring-mode safety boundary without disabling manual intervention.

Update available via HACS
