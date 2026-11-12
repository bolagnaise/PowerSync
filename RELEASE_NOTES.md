<!-- release: v2.12.1682 -->

## EV charging actions for Home Assistant automations

Enable or disable Solar Surplus, Scheduled Charging, Price Level and a vehicle's Smart Schedule through callable PowerSync actions in Home Assistant. These actions preserve your saved charging settings and let an automation disable Scheduled Charging at your chosen home-battery threshold while leaving other charging modes enabled. Changes apply through the normal charging evaluation cycle.

**Existing Solar Surplus automations keep working.** The old switch retains its entity ID and visibility; it is not hidden or removed. New setups use actions instead of adding more EV switches. You can migrate existing automations to the new actions when convenient—no forced migration is required.

Update through HACS and restart Home Assistant. See the [EV charging actions guide](https://github.com/bolagnaise/PowerSync/blob/main/docs/ev-charging-actions.md) for action names, parameters and the Scheduled Charging battery-threshold example. This adds automation controls, not a built-in Scheduled Charging battery cutoff.
