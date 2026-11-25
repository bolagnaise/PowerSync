<!-- release: v2.12.1834 -->

## Stored-energy origin no longer drifts to "unknown" while the battery discharges

PowerSync tracks how much of the energy in your battery came from solar, from the grid, or from an unknown source, and uses that to value stored energy when planning. When the battery's state of charge updated in delayed steps during self-consumption, each step moved some solar and grid energy into "unknown", so the unknown share kept growing through a discharge. Unknown energy is valued at the median import price, so this could make stored energy look more expensive than it was. Discharge now draws every source in proportion, however the state of charge updates.

Update through HACS and restart Home Assistant.
