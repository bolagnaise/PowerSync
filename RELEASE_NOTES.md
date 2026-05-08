<!-- release: v2.12.331 -->

## What's Changed

**NeoVolt SOC-aware stack balancing**
Independent NeoVolt / Bytewatt stack balancing now keeps lower-SOC packs ahead in the charge priority. PowerSync will no longer force-charge a higher-SOC parked stack from surplus while another stack is behind by more than the configured tolerance.

**NeoVolt surplus balancer controls and diagnostics**
NeoVolt setup now includes an independent-stack surplus balancing mode plus SOC balance tolerance. A new `sensor.power_sync_neovolt_surplus_balancer` diagnostic sensor exposes the coordinator state, per-stack SOC, per-stack power, active target, SOC delta, and the reason balancing is waiting or blocked.

Update available via HACS
