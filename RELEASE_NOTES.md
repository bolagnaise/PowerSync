<!-- release: v2.12.808 -->

## What's Changed

**Tesla manual force discharge now reapplies battery export**
Manual Force Discharge for Tesla Powerwall now always reapplies the Fleet API `battery_ok` export rule before disabling grid charging and uploading the temporary force-discharge tariff. This covers cases where Tesla or the Home Assistant Fleet integration reports the intended export setting but the gateway still needs a fresh write before it will actually allow battery discharge/export.

Update available via HACS
