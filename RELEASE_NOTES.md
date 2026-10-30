<!-- release: v2.12.1567 -->

## Avoid a false Powerwall problem alert when the car is waiting

Powerwall local monitoring no longer shows a problem solely because the gateway reports that an EV is allowed to charge but is not charging. This can occur while the vehicle is waiting on its own schedule or has deliberately stopped charging. The status remains available in the alert sensors’ diagnostic attributes.

Other battery and charger alerts remain visible, and an explicit warning or fault severity still raises the problem alert. Update through HACS and restart Home Assistant.
