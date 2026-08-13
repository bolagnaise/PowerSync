<!-- release: v2.12.1091 -->

## What's Changed

**Keep Smart Schedule solar status and battery policy isolated**

Smart Schedule solar charging now keeps its own configured home-battery floor when it takes over an existing Solar Surplus controller, even when standalone Solar Surplus is disabled or has a different battery threshold. The handoff remains command-neutral, so an EV that is already charging is not stopped or restarted. Measured charging status also takes precedence over stale pause markers, preventing the EV card from showing a battery-wait message beside an active Charging state while retaining genuine non-pause error details.

Update available via HACS
