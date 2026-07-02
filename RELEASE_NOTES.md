<!-- release: v2.12.745 -->

## What's Changed

**Preserve coordinator refresh logging**
PowerSync now keeps numeric Home Assistant coordinator timing arguments as numbers while redacting sensitive log values. This prevents the `TypeError: must be real number, not str` logging failure seen during rapid AEMO refreshes.

**Show held optimizer force charge as the active action**
The optimizer status API now reports an active held force charge or discharge as the effective current action even when a new LP solve has moved the planned slot back to self-consumption. This keeps the dashboard current status aligned with the hardware command that is still being held.

Update available via HACS
