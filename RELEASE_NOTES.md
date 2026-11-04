<!-- release: v2.12.1623 -->

## Flow Power Happy Hour Plus

PowerSync now includes a Happy Hour Plus (40c) plan: the first 15 kWh exported between 5:30pm and 9:30pm each day earns 40c/kWh, with additional exports in that window valued at 10c/kWh. The optimizer and price sensors track the remaining daily allowance.

Update through HACS, restart Home Assistant, then select Happy Hour Plus (40c) in Flow Power settings if it matches your written contract. When today's remaining allowance cannot be established from telemetry, PowerSync uses the 10c base rate until coverage is established.
