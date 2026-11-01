<!-- release: v2.12.1582 -->

## Battery status while solar is charging

Fixed the dashboard saying the battery is powering the home while it is charging in self-consumption mode. The heading now follows the measured battery flow, and the optimizer plan labels its current control separately from live power readings. When battery power is unavailable, self-consumption is shown without claiming a physical flow.

Update through HACS, restart Home Assistant and refresh the dashboard. This display correction does not change Powerwall charging limits or battery controls.
