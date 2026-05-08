<!-- release: v2.12.338 -->

## What's Changed

**Fix NeoVolt multi-inverter grid and load reporting**
PowerSync now detects when multiple NeoVolt inverter entries are reporting the same shared site grid meter and avoids adding that reading twice. This prevents force-charge periods from showing inflated grid import and false home-load spikes in the PowerSync sensors and dashboard.

**Improve NeoVolt full-stack balancing**
When one NeoVolt stack is already full and another stack is still catching up, PowerSync now parks the full stack until the lower stack reaches the catch-up threshold. This reduces unnecessary stack fighting near the top of charge while still preserving the normal SOC tolerance behavior for wider imbalances.

Update available via HACS
