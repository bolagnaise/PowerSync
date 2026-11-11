<!-- release: v2.12.1677 -->

## Battery-powered scheduled EV charging

Scheduled EV charging with no grid import now holds its charging rate when the home battery keeps grid flow within the configured tolerance. Small meter fluctuations could previously reduce the charging rate repeatedly, even while the battery had power available.

Inverter limits and reductions for grid imports above the tolerance still apply. Update through HACS and restart Home Assistant.
