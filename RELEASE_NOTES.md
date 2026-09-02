<!-- release: v2.12.1224 -->

## What's Changed

**Reliable GoodWe manual-control errors and cancellation**

Manual GoodWe Charge and Discharge actions now report an error when their EMS command is not confirmed instead of completing silently. Force-control switches now follow the confirmed PowerSync control state, and an unconfirmed normal-operation restore keeps the active state visible while PowerSync makes bounded cleanup retries. This preserves the distinction between an EMS entity acknowledgement and a verified physical battery response.

**GoodWe Home Assistant profile detection**

The Home Assistant GoodWe telemetry and EMS-control profile now recognises the integration's standard `active_power` and `ppv` sensors during setup. This lets the profile accept a complete standard GoodWe telemetry surface instead of falling back to direct IP control solely because its setup validator used different names.

Update available via HACS
