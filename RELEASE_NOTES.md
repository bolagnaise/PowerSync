<!-- release: v2.12.798 -->

## What's Changed

**Grid charge SOC cap now respects cheapest eligible slots**
PowerSync now enforces the Grid Charge SOC Cap inside the optimizer instead of using it to remove future charge windows before the solve. This lets Flow Power and other dynamic-tariff plans compare all eligible import prices, so a 70% grid-charge cap will no longer force earlier, more expensive charging just because those slots appear first.

**Solar can still charge above the grid cap**
The optimizer now tracks grid-to-battery energy separately from solar charging. Forced grid charging is capped at the configured SOC limit, while forecast solar surplus can still fill the battery above that cap for later export or self-consumption.

Update available via HACS
