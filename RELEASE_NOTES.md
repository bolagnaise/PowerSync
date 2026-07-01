<!-- release: v2.12.742 -->

## What's Changed

**Flow Power account-specific PEA pricing**
PowerSync now applies the Flow Power V2 PEA formula with the average daily network tariff adjustment when tariff data is available, matching account-specific network TOU settlement more closely. The Flow Power price sensor also exposes the network TOU adjustment and a price without that adjustment so users can see how the tariff component is affecting the final import rate.

**More resilient Flow Power portal and forecast parsing**
Flow Power account data now normalises numeric billing components from the portal before pricing uses them, prefers a real import BPEA when available, and falls back safely when the import value is reported as zero. Forecast parsing now accepts timestamp-keyed price maps and infers missing forecast timestamps from the first explicit interval instead of collapsing otherwise valid price rows.

**Stateful manual battery controls**
The generated PowerSync dashboard now highlights active manual battery modes. Charge, Discharge, and Hold SoC buttons show the remaining countdown while active, and Self Consumption visibly stays selected until restored.

Update available via HACS
