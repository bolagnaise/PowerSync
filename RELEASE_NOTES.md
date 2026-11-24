<!-- release: v2.12.1820 -->

## Solar and scheduled charging for an already-awake Tesla

With a compatible ESPHome Bluetooth bridge, PowerSync can use a fresh vehicle power reading to confirm that an already-awake, stopped Tesla is responding and proceed with a solar or scheduled charging start. A fresh reading showing the car is idle can also confirm a Stop when it is already stopped, so EV power and Home Load remain available.

## Charge By Time keeps charging and export when the target is out of reach

When the inverter's available modes leave the battery short of its target at the deadline, Charge By Time can now keep a usable grid-charging and export plan. This works with Disable Idle on or off.

Update through HACS and restart Home Assistant.
