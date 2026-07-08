<!-- release: v2.12.790 -->

## What's Changed

**Grid-charge price cap now uses the visible tariff price**
Smart Optimization now applies **Maximum grid charge price** against the user-facing import tariff before Flow Power's far-future LP price decay is applied. This prevents forced grid charging from being planned above the configured buy-price cap when the display tariff is expensive but the internal model has softened future spikes.

**Optimizer command refreshes respect disabled and duplicate states**
PowerSync now stops optimizer-owned force charge/discharge refreshes once Smart Optimization is disabled or another command path has taken ownership, reducing duplicate battery commands and stale re-application after mode changes.

**EV ownership detection handles non-BLE unplug states**
Mixed EV setups now better detect when a non-BLE vehicle is unplugged, and a no-VIN stop request no longer suppresses charging for another discovered vehicle.

**Hold SoC survives restart and unloads cleanly**
Hold SoC state is persisted across Home Assistant restarts, and its timers are cancelled on unload so stale holds do not keep running after PowerSync is reloaded.

Update available via HACS
