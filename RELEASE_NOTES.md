<!-- release: v2.12.1659 -->

## Solar inverter controls

The AC inverter card now provides Load Follow, Stop and Restore directly in the PowerSync dashboard without requiring the HACS button-card. Load Follow requests home-load following or zero export, depending on the inverter; Stop requests zero output; Restore returns normal operation.

Tap an action twice to confirm. Controls are disabled when inverter status is unavailable, offline or in error. A sent request does not confirm a physical change: check inverter status and measured solar power. Update through HACS, restart Home Assistant and refresh the dashboard.
