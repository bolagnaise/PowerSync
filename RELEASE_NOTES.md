<!-- release: v2.12.1637 -->

## Clear frozen Tesla charging status

Fixes a Tesla remaining shown as **Charging** for hours when Home Assistant restarts during a network outage and Tesla Fleet restores old readings without refreshing them. After 30 minutes without a report from an online car, PowerSync stops showing it as charging and displays its power as unknown.

EV load and Home Load readings that include EV charging remain unknown while the car's activity is uncertain, rather than assuming 0 kW or a confirmed stop.

Update through HACS and restart Home Assistant.
