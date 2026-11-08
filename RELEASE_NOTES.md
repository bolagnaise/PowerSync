<!-- release: v2.12.1656 -->

## Multiple cars on one generic charger

The EV Charging dashboard can now save separate vehicle profiles for a shared generic charger. Each car keeps its own battery-percentage sensor, usable battery capacity and Smart Schedule target and departure times. The charger meter is still counted once in the EV/Home load split.

Add your cars under **Shared charger vehicles**, then choose **Connected vehicle** before plugging a car in. Stop charging and unplug before switching cars; active sessions and pending recovery block a change. Selection is manual, and Smart Schedule waits when no car is selected or its battery-percentage sensor is unavailable. Existing single-car setups keep working until profiles are configured.

Update through HACS and restart Home Assistant.
