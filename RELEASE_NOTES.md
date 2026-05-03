## What's Changed

**Solar-surplus EV charging now uses your full configured charging speed**
Tesla's `number.car_charging_amps` entity reports a stale `max=16A` while the car is plugged in but idle, even when your wiring and Home Power settings allow far more. Solar-surplus dynamic charging would clamp every set-amps call down to that 16A ceiling, leaving substantial solar export instead of pumping it into the car. The Home Power "Max charging speed" setting (per-phase amps) is now honored as the authoritative max for solar-surplus mode and overrides the Tesla entity's idle reading. The same override logic is wired through Tesla BLE, Teslemetry Bluetooth, and the Tesla cloud paths so it works regardless of which Tesla integration is in play. The override only kicks in when the entity's reported max is below your configured max — once charging actually starts and the entity reports a higher real limit, that real limit is respected.

For non-solar dynamic modes, default behavior is unchanged: the entity max is still honored, so we don't accidentally exceed a hardware-imposed limit. New regression tests cover both the solar-surplus override path and the default-behavior clamp path.

Update available via HACS
