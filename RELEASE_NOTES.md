## What's Changed

**Follower PW3 Battery Health via Local Gateway**
The battery health query now tries the Powerwall leader's local TEDAPI gateway first (`https://{gateway_ip}/tedapi/v1r`) before falling back to the Fleet API cloud relay. The local gateway has direct CAN bus visibility of all connected Powerwall units — including follower stacks — so the follower's per-pack BMS signals (`BMS_nominalFullPackEnergy`, `BMS_nominalEnergyRemaining`) are populated where the Fleet API relay returned `None`. When the local gateway is unreachable (e.g. away from home), the Fleet API relay is used as before and the follower shows 0 kWh. The `source` field in the battery health response now reflects which path was used: `ha_local_tedapi` or `ha_fleet_api_relay`.

Update available via HACS
