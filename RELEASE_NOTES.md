<!-- release: v2.12.760 -->

## What's Changed

**Fix AC inverter curtailment at 0c export**
PowerSync now treats AC-coupled inverter export below 1c/kWh as uneconomic in the live curtailment guard, matching the configured curtailment threshold and the tariff action plan. This fixes Fronius AC curtailment restoring the inverter during Flow Power 0c export periods when the site was exporting solar and the battery was not absorbing it.

Update available via HACS
