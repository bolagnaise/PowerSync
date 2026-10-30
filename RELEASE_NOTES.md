<!-- release: v2.12.1568 -->

## Configure settings directly from the dashboard

Select the name or chevron beside Smart Optimization, Apply recommended reserve, Cost neutral, Charge by time or Auto update to configure its options. The popup matches the dashboard in both light and dark themes, while the on/off switches remain directly usable.

Changes are saved only when you select Save changes. Auto-update schedule changes require a Home Assistant administrator and keep the existing enabled state.

## Keep solar car charging with the EV controls

The duplicate solar charging switch has been removed from Optimizer settings. Open Car charging → Modes → Solar Surplus → Configure solar charging to set its thresholds, delays and multiple-car preferences. The existing Solar Only button still selects the source for a session started with Start.

Update through HACS, restart Home Assistant and refresh the dashboard to load the new controls.

## Avoid a false Powerwall problem alert when the car is waiting

Powerwall local monitoring no longer shows a problem solely because the gateway reports that an EV is allowed to charge but is not charging. This can occur while the vehicle is waiting on its own schedule or has deliberately stopped charging. The status remains available in the alert sensors’ diagnostic attributes.

Other battery and charger alerts remain visible, and an explicit warning or fault severity still raises the problem alert. Update through HACS and restart Home Assistant.
