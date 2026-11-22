<!-- release: v2.12.1793 -->

## EV charging with or without Smart Optimization

Scheduled Charging and Price Level now use current EPEX, Octopus and Localvolts retail rates when the battery optimizer is off. Missing or future prices cannot authorize a price-limited charge, and disabled Smart Schedule sessions can finish their cleanup when site telemetry is unavailable.

## More accurate Smart Schedule plans

Charging plans account for the actual length of tariff intervals, demand restrictions, overnight departure settings and daylight-saving changes. Multiple cars share the available power, and a car with a deadline can use permitted grid power when another car has reserved the solar.

## Reliable battery and EV coordination

Joint plans use the charger's electrical limits and retain each car's charging ceiling while recalculating. Dynamic EV charging rounds down when another amp would exceed the site import limit. Ending one charging mode preserves any other car's request to protect the home battery.

Disabling Smart Optimization waits for active work before restoring the battery. An interrupted restore retries before the optimizer restarts and respects newer manual or external control.

## Vehicle charge history

New managed charging sessions record the selected car's battery percentage when available. Older sessions retain their recorded values.

Update through HACS and restart Home Assistant.
