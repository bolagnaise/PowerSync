<!-- release: v2.12.1070 -->

## What's Changed

**Solar Surplus now backs down during the stop delay**

When available solar falls below the charger minimum while an EV is already
charging, PowerSync now immediately reduces the charge current to the charger's
real minimum instead of holding the previous, higher target for the full stop
delay. This preserves the configured anti-chatter grace period without
unnecessarily drawing the shortfall from the home battery or grid. Charging
still stops normally if insufficient surplus persists for the configured delay.

Update available via HACS
