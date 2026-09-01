<!-- release: v2.12.1221 -->

## What's Changed

**GoodWe entity-control commands now require state confirmation**

PowerSync now distinguishes a Home Assistant service acknowledgement from a confirmed GoodWe EMS command. Entity-control force, restore, and self-consumption actions require the requested EMS mode and power limit to read back before they are treated as successful. A failed manual Self Consumption restore also clears its temporary control state instead of leaving the dashboard toggle active.

**Failed GoodWe curtailment commands remain safely throttled**

When a direct GoodWe curtail or restore attempt fails, PowerSync keeps the physical-effect status Pending and records the attempt before retrying. Repeated price or telemetry updates therefore wait for the normal retry cadence rather than generating a command storm. A new uneconomic-export episode can still make a fresh attempt.

Update available via HACS
