<!-- release: v2.12.1483 -->
## Powerwall export targets account for home load

The Force Power export slider now respects both the site export limit and the battery power available after home load and solar. For example, a 10 kW battery-output ceiling with 8 kW home load and no solar leaves about 2 kW available for grid export, even when the site export limit is higher.

The available range refreshes with live readings. This corrects the displayed target range; it does not raise the gateway’s timed-command ceiling.
