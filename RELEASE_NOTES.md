<!-- release: v2.12.1697 -->

## Powerwall export at low battery levels

Smart Optimization now switches planned Powerwall export to tariff control at 20% usable charge or lower, including when Local V1R is selected. It releases local control and replans before switching, then restores the original settings when the export action ends.

Your configured reserve and export limits still apply. Tariff control uses Tesla-managed power rather than a precise kW target; manual kW requests continue to use local control.

Update through HACS and restart Home Assistant.
