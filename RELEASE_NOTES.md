<!-- release: v2.12.587 -->

## What's Changed

**Flow Power import planning below reserve**
PowerSync now lets the LP optimizer re-enable later export windows once the forecast can recover the battery above the export floor. This fixes Profit Max cases where a battery starting below the optimizer reserve suppressed export for the whole horizon and therefore never planned the grid-charge import period needed for the Flow Power export window.

**Sigenergy cloud region selection**
Sigenergy Cloud setup now includes a region selector for Australia/New Zealand, Europe, United States, Asia-Pacific, and China. PowerSync uses the matching Sigen Cloud API endpoint, clears cached tokens when the region changes, and updates Device ID guidance for newer or EU accounts that expose user_id, stationId, or stationCode instead of a 13-digit userDeviceId.

**Octopus dynamic restore handling**
Restore-normal handling now treats Octopus as a dynamic pricing provider alongside Amber and Flow Power, so tariff sync behavior is preserved after force-mode cleanup.

Update available via HACS
