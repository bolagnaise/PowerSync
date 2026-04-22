## What's Changed

**Debug Logging for Follower PW3 BMS Data**
The existing `DeviceControllerQuery` response already contains an `esCan.bus.POD` section with per-pack `POD_nom_full_pack_energy` and `POD_nom_energy_remaining` telemetry — one entry per Powerwall unit on the CAN bus, including follower stacks. A debug log of this section has been added to confirm the shape before implementing a parser. This is an internal diagnostic release with no user-visible behaviour change; the follower pack still shows 0 kWh pending the parser in the next release.

Update available via HACS
