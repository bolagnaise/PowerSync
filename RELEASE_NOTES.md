<!-- release: v2.12.1772 -->

## Zaptec charging starts and stops

A Zaptec charger that reports charging at 0 W now shows as waiting for the car to draw power. PowerSync confirms a start when the charger reports power or phase current, and keeps Price Level battery protection while the start is pending. PowerSync checks for a paused session before asking Zaptec to resume and uses phase current when deciding if the charger is already stopped.

PowerSync also checks that installation current changes go to the installation linked to the charger. If your saved pairing is refused, reconnect Zaptec in PowerSync’s Home Assistant integration options.

Update through HACS and restart Home Assistant.
