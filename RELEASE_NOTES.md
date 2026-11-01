<!-- release: v2.12.1590 -->

## Tesla charging alongside native battery control

PowerSync can now use fresh, exactly matched Fleet or Teslemetry vehicle data when a Bluetooth bridge has an old or incomplete charging report. This also lets a stopped vehicle's retained charging reservation clear after two fresh readings, so it does not unnecessarily block a later native battery or EV command.

## Reliable native battery restoration

Native battery charging and export now confirm the exact temporary tariff before reporting success. Cleanup also rechecks the actual tariff before releasing control, including after a restart, so a temporary PowerSync tariff cannot be mistaken for a restored normal tariff. Cleanup remains pending if Tesla cannot confirm restoration.

Update through HACS and restart Home Assistant.
