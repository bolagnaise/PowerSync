<!-- release: v2.12.1640 -->

## Keep deleted EV schedules cleared after restart

Deleting a vehicle charging profile now also removes its saved Smart Schedule and cached battery percentage, so an old enabled schedule cannot return after Home Assistant restarts. Retrying deletion also clears a schedule left behind by an earlier deletion.

Update through HACS and restart Home Assistant, then retry deleting any affected old profile. Vehicles discovered from an active BYD integration and the configured generic charger can still appear in the vehicle list; deleting a charging profile does not disable those sources.
