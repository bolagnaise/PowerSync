<!-- release: v2.12.1613 -->

## Powerwall tariff charging after EV activity

PowerSync now catches fresh stopped-vehicle reports that could fall between background checks and leave an old EV reservation blocking Powerwall tariff charging. This also applies when Tesla charging rate control is off.

Update through HACS and restart Home Assistant. Recovery uses cached reports without waking the vehicle or requesting paid refreshes. Stale data, active charging ownership and unresolved commands continue to block charging until they can be checked safely.
