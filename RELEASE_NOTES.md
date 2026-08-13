<!-- release: v2.12.1086 -->

## What's Changed

**Keep paired Tesla BLE Smart Schedule sessions charging across handoffs**

When a Tesla uses both Fleet/Teslemetry and an explicitly mapped ESPHome BLE bridge, PowerSync now uses that vehicle's fresh local BLE cable state before a stale cloud plug reading. Smart Schedule can restart the same car after a grid-to-solar handoff without falsely reporting it as unplugged. An unavailable BLE bridge still falls back safely to Fleet telemetry, and ambiguous multi-vehicle mappings remain fail-closed.

Update available via HACS
