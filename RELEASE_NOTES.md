<!-- release: v2.12.1108 -->

## What's Changed

**Kept Tesla Home Load available after final sensor normalization**
PowerSync now carries the VIN-scoped Wall Connector reconciliation through the Home Load sensor's canonical normalization step. A valid direct Wall Connector reading can no longer be replaced by the same vehicle's incomplete aggregate observation after the Tesla coordinator has already calculated the correct non-EV household load.

**Preserved fail-closed handling for genuinely separate chargers**
The direct Tesla meter fills only its matching physical vehicle identity. Home Load still becomes unavailable when a different active charger has no usable power measurement, preventing an unmeasured EV load from being reported as household consumption.

Update available via HACS
